"""Codex callback worker execution."""

import time
from collections.abc import Callable
from dataclasses import replace

from sidekick_usages.clock import Clock
from sidekick_usages.core.selection.models import (
    DueOperation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    OperationKind,
    OperationPriority,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.credentials.codex.models import (
    CodexManagedAuthorityResult,
    require_managed_codex_authority,
)
from sidekick_usages.credentials.codex.types import CodexManagedOutcome
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.codex.results import (
    codex_exchange_deadlines_current,
    codex_managed_worker_result,
)
from sidekick_usages.daemon.worker.exchange import WorkerExchangeChannel
from sidekick_usages.daemon.worker.runtime import (
    worker_failure,
    worker_success,
)
from sidekick_usages.persistence.state.files import ManagedStateConflictError
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
    ProviderMutationAuthority,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.codex.broker.external_auth.refresh import (
    decode_codex_callback_acknowledgement,
    decode_codex_callback_instruction,
    encode_codex_refresh_reply,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexCallbackInstruction,
    CodexProjectionExpectation,
)
from sidekick_usages.providers.codex.broker.types import CodexCallbackMode
from sidekick_usages.serialization.framing import clear_mutable_buffer


class CodexCallbackWorkerExecutor:
    """Refresh or rehydrate one selected managed Codex authority."""

    def __init__(
        self,
        coordinator: CodexManagedAuthorityCoordinator,
        selected: SelectedStateStore,
        exchange: WorkerExchangeChannel,
        clock: Clock,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._coordinator = coordinator
        self._selected = selected
        self._exchange = exchange
        self._clock = clock
        self._monotonic = monotonic

    def execute(
        self,
        operation: DueOperation,
        authority: ProviderMutationAuthority,
    ) -> WorkerResult:
        """Execute one exact callback without persisting credential data."""
        authority.require(operation.provider_id)
        account_id = operation.required_account_id
        account_authority = authority.account(account_id)
        if (
            operation.provider_id is not ProviderId.CODEX
            or operation.kind is not OperationKind.CODEX_CALLBACK
            or operation.priority is not OperationPriority.CODEX_CALLBACK
        ):
            raise ValueError("Worker operation is not a Codex callback.")
        try:
            instruction = self._receive_instruction(operation)
            selected = self._require_selected(instruction)
            if instruction.mode is CodexCallbackMode.REFRESH:
                return self._refresh(
                    operation,
                    instruction,
                    selected,
                    account_authority,
                )
            return self._rehydrate(
                operation,
                instruction,
                selected,
                account_authority,
            )
        finally:
            self._exchange.close()

    def _receive_instruction(
        self,
        operation: DueOperation,
    ) -> CodexCallbackInstruction:
        payload = self._exchange.receive_instruction()
        try:
            instruction = decode_codex_callback_instruction(payload)
        finally:
            clear_mutable_buffer(payload)
        if (
            instruction.operation_id != operation.operation_id
            or instruction.account_id != operation.required_account_id
            or not codex_exchange_deadlines_current(
                instruction.deadlines,
                self._monotonic,
            )
        ):
            raise ValueError("Codex callback instruction is stale.")
        return instruction

    def _refresh(
        self,
        operation: DueOperation,
        instruction: CodexCallbackInstruction,
        selected: SelectedAccountState,
        authority: OperationAuthority,
    ) -> WorkerResult:
        expected = _expectation(instruction)
        staged = self._coordinator.stage_refresh_with_authority(
            operation.required_account_id,
            authority,
            expected,
        )
        if isinstance(staged, CodexManagedAuthorityResult):
            return self._managed_failure(operation, staged)
        projection = self._coordinator.open_staged_projection_with_authority(
            staged,
            authority,
        )
        if isinstance(projection, CodexManagedAuthorityResult):
            return self._managed_failure(operation, projection)
        committed = self._coordinator.commit_staged_authority_with_authority(
            staged,
            authority,
        )
        if committed.outcome is not CodexManagedOutcome.HEALTHY:
            return self._managed_failure(operation, committed)
        self._require_selected(instruction, expected=selected)
        with projection:
            response = encode_codex_refresh_reply(
                instruction,
                projection,
            )
            submission = self._exchange.submit(
                response,
                instruction.deadlines.response_deadline_seconds,
                instruction.deadlines.completion_deadline_seconds,
            )
        acknowledgement = submission.receive_acknowledgement()
        try:
            decode_codex_callback_acknowledgement(
                acknowledgement,
                instruction,
                staged.after.generation,
            )
        finally:
            clear_mutable_buffer(acknowledgement)
        if not self._commit_selected(selected, committed):
            return worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "selected_state_changed",
                self._clock,
            )
        return worker_success(operation, self._clock)

    def _rehydrate(
        self,
        operation: DueOperation,
        instruction: CodexCallbackInstruction,
        selected: SelectedAccountState,
        authority: OperationAuthority,
    ) -> WorkerResult:
        staged = self._coordinator.stage_rehydration_with_authority(
            operation.required_account_id,
            authority,
            _expectation(instruction),
        )
        if isinstance(staged, CodexManagedAuthorityResult):
            return self._managed_failure(operation, staged)
        projection = self._coordinator.open_staged_projection_with_authority(
            staged,
            authority,
        )
        if isinstance(projection, CodexManagedAuthorityResult):
            return self._managed_failure(operation, projection)
        committed = self._coordinator.commit_staged_authority_with_authority(
            staged,
            authority,
        )
        if committed.outcome is not CodexManagedOutcome.HEALTHY:
            return self._managed_failure(operation, committed)
        self._require_selected(instruction, expected=selected)
        with projection:
            response = encode_codex_refresh_reply(
                instruction,
                projection,
            )
            submission = self._exchange.submit(
                response,
                instruction.deadlines.response_deadline_seconds,
                instruction.deadlines.completion_deadline_seconds,
            )
        acknowledgement = submission.receive_acknowledgement()
        try:
            decode_codex_callback_acknowledgement(
                acknowledgement,
                instruction,
                projection.generation,
            )
        finally:
            clear_mutable_buffer(acknowledgement)
        if not self._commit_selected(selected, committed):
            return worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "selected_state_changed",
                self._clock,
            )
        return worker_success(operation, self._clock)

    def _require_selected(
        self,
        instruction: CodexCallbackInstruction,
        *,
        expected: SelectedAccountState | None = None,
    ) -> SelectedAccountState:
        selected = self._selected.load(ProviderId.CODEX)
        if (
            selected is None
            or selected.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
            or selected.account_id != instruction.account_id
            or selected.provider_identity != instruction.provider_identity
            or selected.runtime_generation != instruction.source_generation
            or (expected is not None and selected != expected)
        ):
            raise ValueError("Selected Codex authority changed.")
        return selected

    def _commit_selected(
        self,
        selected: SelectedAccountState,
        committed: CodexManagedAuthorityResult,
    ) -> bool:
        try:
            managed = require_managed_codex_authority(committed.account)
        except ValueError:
            return False
        candidate = replace(
            selected,
            runtime_generation=managed.generation,
            verified_at=managed.verified_at,
            outcome=ActivationOutcome.VERIFIED,
        )
        if candidate == selected:
            return True
        try:
            self._selected.compare_and_swap(candidate, expected=selected)
        except ManagedStateConflictError:
            return False
        return True

    def _managed_failure(
        self,
        operation: DueOperation,
        result: CodexManagedAuthorityResult,
    ) -> WorkerResult:
        return codex_managed_worker_result(operation, result, self._clock)


def _expectation(
    instruction: CodexCallbackInstruction,
) -> CodexProjectionExpectation:
    return CodexProjectionExpectation(
        instruction.account_id,
        instruction.provider_identity,
        instruction.source_generation,
    )

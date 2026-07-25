"""Isolated Codex callback execution and post-dispatch commit."""

import time
from collections.abc import Callable
from dataclasses import replace

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    CodexAccountAuthority,
    CodexManagedAuthority,
)
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
)
from sidekick_usages.credentials.codex.types import CodexManagedOutcome
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.exchange import (
    CALLBACK_COMPLETION_TAIL_SECONDS,
    WorkerCallbackChannel,
)
from sidekick_usages.persistence.state.files import ManagedStateConflictError
from sidekick_usages.persistence.supervisor.authority import OperationAuthority
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

_ACTION_REQUIRED_OUTCOMES = frozenset(
    {
        CodexManagedOutcome.INCOMPATIBLE,
        CodexManagedOutcome.LOGGED_OUT,
        CodexManagedOutcome.MALFORMED,
        CodexManagedOutcome.REJECTED,
    }
)


class CodexCallbackWorkerExecutor:
    """Refresh or rehydrate one selected managed Codex authority."""

    def __init__(
        self,
        coordinator: CodexManagedAuthorityCoordinator,
        selected: SelectedStateStore,
        channel: WorkerCallbackChannel,
        clock: Clock,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._coordinator = coordinator
        self._selected = selected
        self._channel = channel
        self._clock = clock
        self._monotonic = monotonic

    def execute(
        self,
        operation: DueOperation,
        authority: OperationAuthority,
    ) -> WorkerResult:
        """Execute one exact callback without persisting credential data."""
        authority.require(operation.account_id)
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
                    authority,
                )
            return self._rehydrate(
                operation,
                instruction,
                selected,
                authority,
            )
        finally:
            self._channel.close()

    def _receive_instruction(
        self,
        operation: DueOperation,
    ) -> CodexCallbackInstruction:
        payload = self._channel.receive_instruction()
        try:
            instruction = decode_codex_callback_instruction(payload)
        finally:
            clear_mutable_buffer(payload)
        if (
            instruction.operation_id != operation.operation_id
            or instruction.account_id != operation.account_id
            or instruction.response_deadline_seconds <= self._monotonic()
            or instruction.completion_deadline_seconds
            < instruction.response_deadline_seconds
            + CALLBACK_COMPLETION_TAIL_SECONDS
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
            operation.account_id,
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
            submission = self._channel.submit(
                response,
                instruction.response_deadline_seconds,
                instruction.completion_deadline_seconds,
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
            return self._failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "selected_state_changed",
            )
        return self._success(operation)

    def _rehydrate(
        self,
        operation: DueOperation,
        instruction: CodexCallbackInstruction,
        selected: SelectedAccountState,
        authority: OperationAuthority,
    ) -> WorkerResult:
        staged = self._coordinator.stage_rehydration_with_authority(
            operation.account_id,
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
            submission = self._channel.submit(
                response,
                instruction.response_deadline_seconds,
                instruction.completion_deadline_seconds,
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
            return self._failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "selected_state_changed",
            )
        return self._success(operation)

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
        authority = committed.account.authority
        if not isinstance(authority, CodexAccountAuthority):
            return False
        managed = authority.subscription
        if not isinstance(managed, CodexManagedAuthority):
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
        outcome = (
            WorkerOutcome.ACTION_REQUIRED
            if result.outcome in _ACTION_REQUIRED_OUTCOMES
            else (
                WorkerOutcome.TIMED_OUT
                if result.outcome is CodexManagedOutcome.TIMED_OUT
                else WorkerOutcome.TRANSIENT_FAILURE
            )
        )
        return self._failure(
            operation,
            outcome,
            f"codex_managed_{result.outcome.value}",
        )

    def _success(self, operation: DueOperation) -> WorkerResult:
        return WorkerResult(
            operation_id=operation.operation_id,
            outcome=WorkerOutcome.SUCCEEDED,
            finished_at=self._clock.now(),
        )

    def _failure(
        self,
        operation: DueOperation,
        outcome: WorkerOutcome,
        code: str,
    ) -> WorkerResult:
        return WorkerResult(
            operation_id=operation.operation_id,
            outcome=outcome,
            finished_at=self._clock.now(),
            failure_code=code,
        )


def _expectation(
    instruction: CodexCallbackInstruction,
) -> CodexProjectionExpectation:
    return CodexProjectionExpectation(
        instruction.account_id,
        instruction.provider_identity,
        instruction.source_generation,
    )

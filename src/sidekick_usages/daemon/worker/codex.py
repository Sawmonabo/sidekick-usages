"""Isolated Codex callback execution and post-dispatch commit."""

import time
from collections.abc import Callable
from dataclasses import replace

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
    SidekickAccountId,
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
from sidekick_usages.credentials.codex.activation import (
    CodexActivationError,
    CodexActivationService,
)
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.credentials.codex.models import (
    CodexManagedAuthorityResult,
    require_managed_codex_authority,
)
from sidekick_usages.credentials.codex.reconciliation import (
    CodexNativeReconciliationError,
    CodexNativeReconciliationService,
)
from sidekick_usages.credentials.codex.types import (
    CodexActivationFailure,
    CodexManagedOutcome,
)
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.exchange import (
    WORKER_EXCHANGE_COMPLETION_TAIL_SECONDS,
    WorkerExchangeChannel,
    WorkerExchangeError,
)
from sidekick_usages.daemon.worker.ports import AccountMetricsCollector
from sidekick_usages.persistence.state.files import ManagedStateConflictError
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
    ProviderMutationAuthority,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.external_auth.activation import (
    decode_codex_activation_acknowledgement,
    decode_codex_activation_instruction,
    encode_codex_activation_reply,
)
from sidekick_usages.providers.codex.broker.external_auth.refresh import (
    decode_codex_callback_acknowledgement,
    decode_codex_callback_instruction,
    encode_codex_refresh_reply,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexActivationInstruction,
    CodexCallbackInstruction,
    CodexExchangeDeadlines,
    CodexProjectionExpectation,
    CodexProjectionReceipt,
)
from sidekick_usages.providers.codex.broker.ports import CodexProjection
from sidekick_usages.providers.codex.broker.types import (
    CodexActivationMode,
    CodexBrokerFailure,
    CodexCallbackMode,
)
from sidekick_usages.serialization.framing import clear_mutable_buffer
from sidekick_usages.usage.models import activity_has_failure


class CodexManagedMaintenanceWorkerExecutor:
    """Maintain one managed Codex authority under its account lock."""

    def __init__(
        self,
        coordinator: CodexManagedAuthorityCoordinator,
        metrics: AccountMetricsCollector,
        clock: Clock,
    ) -> None:
        self._coordinator = coordinator
        self._metrics = metrics
        self._clock = clock

    def execute(
        self,
        operation: DueOperation,
        authority: OperationAuthority,
    ) -> WorkerResult:
        """Run one scheduled maintenance or explicit forced refresh."""
        authority.require(operation.required_account_id)
        scheduled = (
            operation.kind is OperationKind.MAINTAIN
            and operation.priority is OperationPriority.SCHEDULED
        )
        forced = (
            operation.kind is OperationKind.REFRESH
            and operation.priority is OperationPriority.INTERACTIVE
        )
        if operation.provider_id is not ProviderId.CODEX or not (
            scheduled or forced
        ):
            raise ValueError(
                "Worker operation is not managed Codex maintenance."
            )
        result = (
            self._coordinator.refresh_with_authority(
                operation.required_account_id,
                authority,
            )
            if forced
            else self._coordinator.maintain_with_authority(
                operation.required_account_id,
                authority,
            )
        )
        if result.outcome is not CodexManagedOutcome.HEALTHY:
            return _managed_worker_result(operation, result, self._clock)
        metrics = self._metrics.collect(
            operation.required_account_id,
            authority,
        )
        if metrics.failures:
            return _worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "codex_metrics_" + metrics.failures[0].kind.value,
                self._clock,
            )
        if any(activity_has_failure(item) for item in metrics.activities):
            return _worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "codex_activity_unavailable",
                self._clock,
            )
        return _worker_success(operation, self._clock)


class CodexActivationWorkerExecutor:
    """Run one journaled activation through the resident runtime broker."""

    def __init__(
        self,
        service: CodexActivationService,
        exchange: WorkerExchangeChannel,
        clock: Clock,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._exchange = exchange
        self._clock = clock
        self._monotonic = monotonic

    def execute(
        self,
        operation: DueOperation,
        authority: ProviderMutationAuthority,
    ) -> WorkerResult:
        """Execute one exact activation or journal recovery."""
        authority.require(operation.provider_id)
        if (
            operation.provider_id is not ProviderId.CODEX
            or operation.kind
            not in {OperationKind.ACTIVATE, OperationKind.RECONCILE}
            or operation.priority is not OperationPriority.INTERACTIVE
        ):
            raise ValueError("Worker operation is not a Codex activation.")
        try:
            instruction = self._receive_instruction(operation)
            installer = _CodexActivationInstaller(
                instruction,
                self._exchange,
            )
            if instruction.mode is CodexActivationMode.ACTIVATE:
                self._service.activate(
                    operation.operation_id,
                    operation.required_account_id,
                    authority,
                    installer,
                )
            else:
                self._service.recover(
                    operation.operation_id,
                    operation.required_account_id,
                    authority,
                    installer,
                )
            return _worker_success(operation, self._clock)
        except CodexActivationError as error:
            return _worker_failure(
                operation,
                (
                    WorkerOutcome.ACTION_REQUIRED
                    if error.action_required
                    else WorkerOutcome.TRANSIENT_FAILURE
                ),
                error.failure.value,
                self._clock,
            )
        except CodexBrokerError, WorkerExchangeError:
            return _worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                CodexActivationFailure.DAEMON_UNAVAILABLE.value,
                self._clock,
            )
        except ManagedStateConflictError:
            return _worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                CodexActivationFailure.STATE_CHANGED.value,
                self._clock,
            )
        finally:
            self._exchange.close()

    def _receive_instruction(
        self,
        operation: DueOperation,
    ) -> CodexActivationInstruction:
        payload = self._exchange.receive_instruction()
        try:
            instruction = decode_codex_activation_instruction(payload)
        finally:
            clear_mutable_buffer(payload)
        expected_mode = (
            CodexActivationMode.ACTIVATE
            if operation.kind is OperationKind.ACTIVATE
            else CodexActivationMode.RECOVER
        )
        if (
            instruction.operation_id != operation.operation_id
            or instruction.account_id != operation.required_account_id
            or instruction.mode is not expected_mode
            or not _deadlines_current(
                instruction.deadlines,
                self._monotonic,
            )
        ):
            raise ValueError("Codex activation instruction is stale.")
        return instruction


class _CodexActivationInstaller:
    """Project one worker-held token through its inherited exchange."""

    def __init__(
        self,
        instruction: CodexActivationInstruction,
        exchange: WorkerExchangeChannel,
    ) -> None:
        self._instruction = instruction
        self._exchange = exchange
        self._expectation: CodexProjectionExpectation | None = None
        self._submitted = False

    def prepare(
        self,
        account_id: SidekickAccountId,
        provider_identity: ProviderIdentity,
        generation: AuthorityGeneration,
    ) -> CodexProjectionReceipt | None:
        """Bind the expected projection before credential access."""
        if (
            self._expectation is not None
            or self._submitted
            or not self._instruction.permits(account_id)
        ):
            raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
        self._expectation = CodexProjectionExpectation(
            account_id,
            provider_identity,
            generation,
        )
        return None

    def install(
        self,
        projection: CodexProjection,
    ) -> CodexProjectionReceipt:
        """Send one projection and require its exact official receipt."""
        expectation = self._expectation
        if (
            expectation is None
            or self._submitted
            or projection.account_id != expectation.account_id
            or projection.provider_identity != expectation.provider_identity
            or projection.generation != expectation.generation
        ):
            raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
        self._submitted = True
        response = encode_codex_activation_reply(
            self._instruction,
            projection,
        )
        submission = self._exchange.submit(
            response,
            self._instruction.deadlines.response_deadline_seconds,
            self._instruction.deadlines.completion_deadline_seconds,
        )
        acknowledgement = submission.receive_acknowledgement()
        try:
            return decode_codex_activation_acknowledgement(
                acknowledgement,
                self._instruction,
                projection.provider_identity,
                projection.generation,
            ).receipt
        finally:
            clear_mutable_buffer(acknowledgement)


class CodexNativeReconciliationWorkerExecutor:
    """Relate one effective Codex runtime observation without credentials."""

    def __init__(
        self,
        service: CodexNativeReconciliationService,
        observations: RuntimeAuthObservationStore,
        clock: Clock,
    ) -> None:
        self._service = service
        self._observations = observations
        self._clock = clock

    def execute(
        self,
        operation: DueOperation,
        authority: ProviderMutationAuthority,
    ) -> WorkerResult:
        """Reconcile one exact provider-scoped observation."""
        authority.require(operation.provider_id)
        if (
            operation.provider_id is not ProviderId.CODEX
            or operation.kind is not OperationKind.RECONCILE_NATIVE
            or operation.account_id is not None
        ):
            raise ValueError(
                "Worker operation is not native Codex reconciliation."
            )
        observation = self._observations.load(ProviderId.CODEX)
        if observation is None:
            return _worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "native_observation_missing",
                self._clock,
            )
        try:
            selected = self._service.reconcile(observation, authority)
        except CodexNativeReconciliationError as error:
            return _worker_failure(
                operation,
                (
                    WorkerOutcome.ACTION_REQUIRED
                    if error.action_required
                    else WorkerOutcome.TRANSIENT_FAILURE
                ),
                error.code,
                self._clock,
            )
        except ManagedStateConflictError:
            return _worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "native_state_changed",
                self._clock,
            )
        if self._observations.load(ProviderId.CODEX) != observation:
            return _worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "native_observation_changed",
                self._clock,
            )
        return self._selected_result(operation, selected)

    def _selected_result(
        self,
        operation: DueOperation,
        selected: SelectedAccountState | None,
    ) -> WorkerResult:
        if (
            selected is not None
            and selected.runtime_state is ProviderRuntimeState.UNREADABLE
        ):
            return _worker_failure(
                operation,
                WorkerOutcome.ACTION_REQUIRED,
                "native_auth_unreadable",
                self._clock,
            )
        if (
            selected is not None
            and selected.runtime_state is ProviderRuntimeState.UNSUPPORTED
        ):
            return _worker_failure(
                operation,
                WorkerOutcome.ACTION_REQUIRED,
                "native_auth_unsupported",
                self._clock,
            )
        return _worker_success(operation, self._clock)


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
            or not _deadlines_current(
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
            return _worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "selected_state_changed",
                self._clock,
            )
        return _worker_success(operation, self._clock)

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
            return _worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                "selected_state_changed",
                self._clock,
            )
        return _worker_success(operation, self._clock)

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
        return _managed_worker_result(operation, result, self._clock)


def _managed_worker_result(
    operation: DueOperation,
    result: CodexManagedAuthorityResult,
    clock: Clock,
) -> WorkerResult:
    if result.outcome is CodexManagedOutcome.HEALTHY:
        return _worker_success(operation, clock)
    outcome = (
        WorkerOutcome.ACTION_REQUIRED
        if result.outcome.action_required
        else (
            WorkerOutcome.TIMED_OUT
            if result.outcome is CodexManagedOutcome.TIMED_OUT
            else WorkerOutcome.TRANSIENT_FAILURE
        )
    )
    return _worker_failure(
        operation,
        outcome,
        f"codex_managed_{result.outcome.value}",
        clock,
    )


def _worker_success(
    operation: DueOperation,
    clock: Clock,
) -> WorkerResult:
    return WorkerResult(
        operation_id=operation.operation_id,
        outcome=WorkerOutcome.SUCCEEDED,
        finished_at=clock.now(),
    )


def _worker_failure(
    operation: DueOperation,
    outcome: WorkerOutcome,
    code: str,
    clock: Clock,
) -> WorkerResult:
    return WorkerResult(
        operation_id=operation.operation_id,
        outcome=outcome,
        finished_at=clock.now(),
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


def _deadlines_current(
    deadlines: CodexExchangeDeadlines,
    monotonic: Callable[[], float],
) -> bool:
    return (
        deadlines.response_deadline_seconds > monotonic()
        and deadlines.completion_deadline_seconds
        >= deadlines.response_deadline_seconds
        + WORKER_EXCHANGE_COMPLETION_TAIL_SECONDS
    )

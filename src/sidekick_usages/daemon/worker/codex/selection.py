"""Isolated Codex global-selection execution."""

import time
from collections.abc import Callable

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.selection.models import (
    DueOperation,
    FinalizedSelection,
    OpenSelectionOperation,
    SelectionAuthorityObservation,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    SelectionCode,
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
from sidekick_usages.daemon.worker.codex.results import (
    codex_exchange_deadlines_current,
)
from sidekick_usages.daemon.worker.exchange import (
    WorkerExchangeChannel,
    WorkerExchangeError,
)
from sidekick_usages.daemon.worker.runtime import (
    selection_worker_success,
    worker_failure,
)
from sidekick_usages.persistence.state.files import ManagedStateConflictError
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexProcessGroupPolicy,
)
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.external_auth.selection import (
    CodexSelectionAcknowledgement,
    CodexSelectionBinding,
    CodexSelectionInstruction,
    decode_codex_selection_acknowledgement,
    decode_codex_selection_instruction,
    encode_codex_selection_reply,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexProjectionExpectation,
)
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from sidekick_usages.serialization.framing import clear_mutable_buffer

_CODEX_SELECTION_KINDS = frozenset(
    {
        OperationKind.SELECTION_PREVALIDATE,
        OperationKind.SELECTION_COMMIT,
        OperationKind.SELECTION_READBACK,
    }
)
_MANAGED_SELECTION_FAILURES = {
    CodexManagedOutcome.UNCHANGED: (
        WorkerOutcome.ACTION_REQUIRED,
        SelectionCode.TARGET_REFRESH_REQUIRED,
    ),
    CodexManagedOutcome.REJECTED: (
        WorkerOutcome.ACTION_REQUIRED,
        SelectionCode.TARGET_REJECTED,
    ),
    CodexManagedOutcome.LOGGED_OUT: (
        WorkerOutcome.ACTION_REQUIRED,
        SelectionCode.TARGET_REFRESH_REQUIRED,
    ),
    CodexManagedOutcome.INCOMPATIBLE: (
        WorkerOutcome.UNSUPPORTED,
        SelectionCode.UNSUPPORTED_PROVIDER_VERSION,
    ),
    CodexManagedOutcome.MALFORMED: (
        WorkerOutcome.ACTION_REQUIRED,
        SelectionCode.TARGET_MALFORMED,
    ),
    CodexManagedOutcome.TIMED_OUT: (
        WorkerOutcome.TIMED_OUT,
        SelectionCode.ACTIVE_OPERATION_TIMEOUT,
    ),
    CodexManagedOutcome.TRANSIENT: (
        WorkerOutcome.TRANSIENT_FAILURE,
        SelectionCode.TARGET_UNREADABLE,
    ),
}
_BROKER_SELECTION_FAILURES = {
    CodexBrokerFailure.PROTOCOL_UNSUPPORTED: (
        WorkerOutcome.UNSUPPORTED,
        SelectionCode.UNSUPPORTED_SESSION_CAPABILITY,
    ),
    CodexBrokerFailure.VERSION_UNSUPPORTED: (
        WorkerOutcome.UNSUPPORTED,
        SelectionCode.UNSUPPORTED_PROVIDER_VERSION,
    ),
    CodexBrokerFailure.SESSION_CONFIGURATION_REQUIRED: (
        WorkerOutcome.ACTION_REQUIRED,
        SelectionCode.SESSION_CONFIGURATION_REQUIRED,
    ),
}


class CodexSelectionWorkerExecutor:
    """Run Codex selection through one resident broker exchange."""

    def __init__(
        self,
        coordinator: CodexManagedAuthorityCoordinator,
        exchange: WorkerExchangeChannel,
        clock: Clock,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._coordinator = coordinator
        self._exchange = exchange
        self._clock = clock
        self._monotonic = monotonic

    def execute_selection(
        self,
        operation: DueOperation,
        active: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
        authority: ProviderMutationAuthority,
    ) -> WorkerResult:
        """Execute one exact journal-bound Codex selection phase."""
        self._require_operation(operation, active, authority)
        try:
            instruction = self._receive_instruction(operation, active)
            if operation.kind is OperationKind.SELECTION_PREVALIDATE:
                refreshed = self._coordinator.refresh_with_authority(
                    active.target_account_id,
                    authority.account(active.target_account_id),
                    process_group=CodexProcessGroupPolicy.INHERITED,
                )
                if refreshed.outcome is not CodexManagedOutcome.HEALTHY:
                    raise _ManagedSelectionError(refreshed)
            target = self._expectation(active.target_account_id, authority)
            if isinstance(target, CodexManagedAuthorityResult):
                raise _ManagedSelectionError(target)
            if (
                operation.kind is OperationKind.SELECTION_COMMIT
                and active.prepared_generation != target.generation
            ):
                raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
            binding = CodexSelectionBinding(
                worker_operation_id=operation.operation_id,
                operation_id=active.operation_id,
                kind=operation.kind,
                pending_epoch=active.pending_epoch,
                account_id=target.account_id,
                provider_identity=target.provider_identity,
                generation=target.generation,
                socket_device=instruction.socket_device,
                socket_inode=instruction.socket_inode,
            )
            acknowledgement = self._exchange_phase(
                instruction,
                binding,
                active,
                baseline,
                authority,
            )
            observation = SelectionAuthorityObservation(
                provider_id=ProviderId.CODEX,
                account_id=acknowledgement.observed_account_id,
                generation=acknowledgement.observed_generation,
            )
            if operation.kind is not OperationKind.SELECTION_READBACK and (
                observation.account_id != active.target_account_id
                or observation.generation != target.generation
            ):
                raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
            return selection_worker_success(
                operation,
                active.pending_epoch,
                observation,
                self._clock,
            )
        except CodexBrokerError as error:
            return self._broker_failure(operation, error)
        except _ManagedSelectionError as error:
            return self._managed_failure(operation, error.result)
        except WorkerExchangeError:
            return self._failure(
                operation,
                SelectionCode.PROVIDER_UNAVAILABLE,
            )
        except ManagedStateConflictError, RuntimeError, ValueError:
            return self._failure(
                operation,
                SelectionCode.AUTHORITY_PROOF_FAILED,
            )
        finally:
            self._exchange.close()

    def _exchange_phase(
        self,
        instruction: CodexSelectionInstruction,
        binding: CodexSelectionBinding,
        active: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
        authority: ProviderMutationAuthority,
    ) -> CodexSelectionAcknowledgement:
        projection = None
        baseline_expectation = None
        if instruction.kind is OperationKind.SELECTION_COMMIT:
            opened = self._coordinator.open_projection_with_authority(
                active.target_account_id,
                authority.account(active.target_account_id),
            )
            if isinstance(opened, CodexManagedAuthorityResult):
                raise _ManagedSelectionError(opened)
            projection = opened
        elif (
            instruction.kind is OperationKind.SELECTION_READBACK
            and baseline is not None
        ):
            opened_baseline = self._expectation(
                baseline.account_id,
                authority,
            )
            if isinstance(opened_baseline, CodexProjectionExpectation):
                baseline_expectation = opened_baseline
        if projection is None:
            response = encode_codex_selection_reply(
                instruction,
                binding,
                baseline=baseline_expectation,
            )
        else:
            with projection:
                response = encode_codex_selection_reply(
                    instruction,
                    binding,
                    projection=projection,
                )
        submission = self._exchange.submit(
            response,
            instruction.deadlines.response_deadline_seconds,
            instruction.deadlines.completion_deadline_seconds,
        )
        payload = submission.receive_acknowledgement()
        try:
            return decode_codex_selection_acknowledgement(payload, binding)
        finally:
            clear_mutable_buffer(payload)

    def _receive_instruction(
        self,
        operation: DueOperation,
        active: OpenSelectionOperation,
    ) -> CodexSelectionInstruction:
        payload = self._exchange.receive_instruction()
        try:
            instruction = decode_codex_selection_instruction(payload)
        finally:
            clear_mutable_buffer(payload)
        if (
            instruction.worker_operation_id != operation.operation_id
            or instruction.operation_id != active.operation_id
            or instruction.kind is not operation.kind
            or instruction.account_id != operation.required_account_id
            or not codex_exchange_deadlines_current(
                instruction.deadlines,
                self._monotonic,
            )
        ):
            raise ValueError("Codex selection instruction is stale.")
        return instruction

    def _expectation(
        self,
        account_id: SidekickAccountId,
        authority: ProviderMutationAuthority,
    ) -> CodexProjectionExpectation | CodexManagedAuthorityResult:
        return self._coordinator.projection_expectation_with_authority(
            account_id,
            authority.account(account_id),
        )

    def _managed_failure(
        self,
        operation: DueOperation,
        result: CodexManagedAuthorityResult,
    ) -> WorkerResult:
        if operation.kind is OperationKind.SELECTION_READBACK:
            return self._failure(
                operation,
                SelectionCode.SELECTION_RECOVERY_REQUIRED,
            )
        outcome, code = _MANAGED_SELECTION_FAILURES[result.outcome]
        return worker_failure(
            operation,
            outcome,
            code.value,
            self._clock,
        )

    def _broker_failure(
        self,
        operation: DueOperation,
        error: CodexBrokerError,
    ) -> WorkerResult:
        if operation.kind is OperationKind.SELECTION_READBACK:
            return self._failure(
                operation,
                SelectionCode.SELECTION_RECOVERY_REQUIRED,
            )
        outcome, code = _BROKER_SELECTION_FAILURES.get(
            error.code,
            (
                WorkerOutcome.TRANSIENT_FAILURE,
                SelectionCode.AUTHORITY_PROOF_FAILED,
            ),
        )
        return worker_failure(
            operation,
            outcome,
            code.value,
            self._clock,
        )

    def _failure(
        self,
        operation: DueOperation,
        code: SelectionCode,
    ) -> WorkerResult:
        return worker_failure(
            operation,
            WorkerOutcome.TRANSIENT_FAILURE,
            code.value,
            self._clock,
        )

    @staticmethod
    def _require_operation(
        operation: DueOperation,
        active: OpenSelectionOperation,
        authority: ProviderMutationAuthority,
    ) -> None:
        authority.require(ProviderId.CODEX)
        if (
            operation.provider_id is not ProviderId.CODEX
            or operation.kind not in _CODEX_SELECTION_KINDS
            or operation.priority is not OperationPriority.INTERACTIVE
            or active.provider_id is not ProviderId.CODEX
            or operation.required_selection_operation_id != active.operation_id
            or operation.required_account_id != active.target_account_id
        ):
            raise ValueError("Worker operation is not Codex selection work.")


class _ManagedSelectionError(RuntimeError):
    """Carry one already-sanitized managed authority refusal."""

    def __init__(self, result: CodexManagedAuthorityResult) -> None:
        super().__init__("Managed Codex selection authority is unavailable.")
        self.result = result

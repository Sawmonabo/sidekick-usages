"""Codex activation worker execution."""

import time
from collections.abc import Callable

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.activation import (
    CodexActivationError,
    CodexActivationService,
)
from sidekick_usages.credentials.codex.types import CodexActivationFailure
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
    worker_failure,
    worker_success,
)
from sidekick_usages.persistence.state.files import ManagedStateConflictError
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.external_auth.activation import (
    decode_codex_activation_acknowledgement,
    decode_codex_activation_instruction,
    encode_codex_activation_reply,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexActivationInstruction,
    CodexProjectionExpectation,
    CodexProjectionReceipt,
)
from sidekick_usages.providers.codex.broker.ports import CodexProjection
from sidekick_usages.providers.codex.broker.types import (
    CodexActivationMode,
    CodexBrokerFailure,
)
from sidekick_usages.serialization.framing import clear_mutable_buffer


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
            return worker_success(operation, self._clock)
        except CodexActivationError as error:
            return worker_failure(
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
            return worker_failure(
                operation,
                WorkerOutcome.TRANSIENT_FAILURE,
                CodexActivationFailure.DAEMON_UNAVAILABLE.value,
                self._clock,
            )
        except ManagedStateConflictError:
            return worker_failure(
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
            or not codex_exchange_deadlines_current(
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

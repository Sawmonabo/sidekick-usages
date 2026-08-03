"""Resident broker ownership for global Codex selection exchanges."""

import time
from collections.abc import Callable
from datetime import datetime
from threading import Lock

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    FinalizedSelection,
    ProviderAuthObservation,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.external_auth.selection import (
    CodexSelectionAcknowledgement,
    CodexSelectionInstruction,
    CodexSelectionReply,
    decode_codex_selection_reply,
    encode_codex_selection_acknowledgement,
    encode_codex_selection_instruction,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexDaemonAuthority,
    CodexExchangeDeadlines,
    CodexProjectionExpectation,
    CodexProjectionReceipt,
)
from sidekick_usages.providers.codex.broker.ports import (
    CodexRuntimeStateReader,
    CodexSavedAuthorityRelation,
    CodexWorkerExchange,
    CodexWorkerExchangeFactory,
)
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from sidekick_usages.serialization.framing import clear_mutable_buffer

CODEX_SELECTION_RESPONSE_SECONDS = 90.0
CODEX_SELECTION_COMPLETION_SECONDS = 120.0
CODEX_SELECTION_INSTALL_RESERVE_SECONDS = 0.5
_NANOSECONDS_PER_SECOND = 1_000_000_000


class CodexSelectionBroker:
    """Bind selection workers to the current qualified resident socket."""

    def __init__(
        self,
        exchanges: CodexWorkerExchangeFactory,
        saved_authority: CodexSavedAuthorityRelation,
        runtime_state: CodexRuntimeStateReader,
        *,
        wall_time: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._exchanges = exchanges
        self._saved_authority = saved_authority
        self._runtime_state = runtime_state
        self._wall_time = wall_time
        self._monotonic = monotonic
        self._lock = Lock()
        self._authority: CodexDaemonAuthority | None = None
        self._instruction: CodexSelectionInstruction | None = None
        self._exchange: CodexWorkerExchange | None = None
        self._readback_projection: ProviderAuthObservation | None = None

    def set_authority(self, authority: CodexDaemonAuthority | None) -> None:
        """Publish the current immutable resident socket qualification."""
        with self._lock:
            self._authority = authority

    def prepare(self, operation: DueOperation) -> bool:
        """Prepare one exact selection exchange against the live socket."""
        if (
            operation.provider_id is not ProviderId.CODEX
            or not operation.kind.is_selection_worker
            or operation.priority is not OperationPriority.INTERACTIVE
        ):
            return False
        with self._lock:
            current = self._instruction
            authority = self._authority
            if current is not None:
                return current.worker_operation_id == operation.operation_id
            if authority is None:
                return False
            projection = None
            if operation.kind is OperationKind.SELECTION_READBACK:
                try:
                    projection = self._runtime_state.current().projection_auth
                except RuntimeError:
                    projection = None
                if projection is None:
                    return False
            started_at = self._monotonic()
            response_deadline = started_at + CODEX_SELECTION_RESPONSE_SECONDS
            completion_deadline = (
                started_at + CODEX_SELECTION_COMPLETION_SECONDS
            )
            instruction = CodexSelectionInstruction(
                worker_operation_id=operation.operation_id,
                operation_id=operation.required_selection_operation_id,
                kind=operation.kind,
                account_id=operation.required_account_id,
                socket_device=authority.control_socket.device,
                socket_inode=authority.control_socket.inode,
                deadlines=CodexExchangeDeadlines(
                    int(response_deadline * _NANOSECONDS_PER_SECOND),
                    int(completion_deadline * _NANOSECONDS_PER_SECOND),
                ),
            )
            encoded = encode_codex_selection_instruction(instruction)
            try:
                exchange = self._exchanges.create(
                    operation.operation_id,
                    encoded,
                    response_deadline,
                    completion_deadline,
                )
            except RuntimeError:
                return False
            self._instruction = instruction
            self._exchange = exchange
            self._readback_projection = projection
            return True

    def pending(
        self,
    ) -> tuple[CodexSelectionInstruction, CodexWorkerExchange] | None:
        """Return the current immutable selection exchange pair."""
        with self._lock:
            instruction = self._instruction
            exchange = self._exchange
        if instruction is None or exchange is None:
            return None
        return instruction, exchange

    def serve(
        self,
        runtime: CodexSharedRuntime,
        instruction: CodexSelectionInstruction,
        exchange: CodexWorkerExchange,
        record_projection: Callable[
            [CodexSharedRuntime, CodexProjectionReceipt],
            None,
        ],
    ) -> None:
        """Serve one worker response under its exact socket binding."""
        self._require_runtime(runtime, instruction)
        payload = exchange.receive_response()
        try:
            reply = decode_codex_selection_reply(payload, instruction)
        finally:
            clear_mutable_buffer(payload)
        if instruction.kind is OperationKind.SELECTION_PREVALIDATE:
            runtime.require_mcp_quiescent()
            observed_account_id = reply.binding.account_id
            observed_generation = reply.binding.generation
        elif instruction.kind is OperationKind.SELECTION_COMMIT:
            projection = reply.projection
            if projection is None:
                raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED)
            self._require_runtime(runtime, instruction)
            with projection:
                runtime.prepare(
                    projection.account_id,
                    projection.provider_identity,
                    projection.generation,
                )
                runtime.require_authority(
                    instruction.socket_device,
                    instruction.socket_inode,
                )
                receipt = runtime.install(
                    projection,
                    deadline=(
                        instruction.deadlines.completion_deadline_seconds
                        - CODEX_SELECTION_INSTALL_RESERVE_SECONDS
                    ),
                )
            self._require_runtime(runtime, instruction)
            record_projection(runtime, receipt)
            observed_account_id = receipt.account_id
            observed_generation = receipt.generation
        else:
            observed_account_id, observed_generation = self._readback(
                runtime,
                reply,
                instruction,
            )
        exchange.acknowledge(
            encode_codex_selection_acknowledgement(
                CodexSelectionAcknowledgement(
                    binding=reply.binding,
                    observed_account_id=observed_account_id,
                    observed_generation=observed_generation,
                )
            )
        )
        if not exchange.wait_for_completion():
            raise RuntimeError("Codex selection worker did not commit.")

    def serve_pending(
        self,
        runtime: CodexSharedRuntime,
        record_projection: Callable[
            [CodexSharedRuntime, CodexProjectionReceipt],
            None,
        ],
        set_ready: Callable[[bool], None],
        wait: Callable[[float], bool],
        status_changed: Callable[[], None] | None,
        *,
        wait_seconds: float,
    ) -> bool:
        """Serve or await one pending selection worker response."""
        pending = self.pending()
        if pending is None:
            return False
        instruction, exchange = pending
        try:
            response_available = exchange.response_available()
        except RuntimeError:
            self.finish(instruction.worker_operation_id, completed=False)
            if status_changed is not None:
                status_changed()
            return True
        if not response_available:
            wait(wait_seconds)
            return True
        completed = False
        if instruction.kind is OperationKind.SELECTION_COMMIT:
            set_ready(False)
        try:
            self.serve(
                runtime,
                instruction,
                exchange,
                record_projection,
            )
            completed = True
        finally:
            if not completed:
                self.set_authority(None)
            self.finish(
                instruction.worker_operation_id,
                completed=completed,
            )
            if status_changed is not None:
                status_changed()
        return True

    def callback_selection(
        self,
        expectation: CodexProjectionExpectation,
    ) -> FinalizedSelection:
        """Return the exact finalized authority for one callback."""
        selected = self._runtime_state.current().finalized_selection
        if (
            selected is None
            or selected.provider_id is not ProviderId.CODEX
            or selected.account_id != expectation.account_id
            or selected.generation != expectation.generation
            or self._saved_authority.expectation(selected) != expectation
        ):
            raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
        return selected

    def finish(
        self,
        worker_operation_id: OperationId,
        *,
        completed: bool,
    ) -> None:
        """Clear one exchange, cancelling it only before completion."""
        if not completed:
            self._exchanges.cancel(worker_operation_id)
        with self._lock:
            instruction = self._instruction
            if (
                instruction is not None
                and instruction.worker_operation_id == worker_operation_id
            ):
                self._instruction = None
                self._exchange = None
                self._readback_projection = None

    def cancel(self) -> None:
        """Cancel the pending selection exchange during broker shutdown."""
        with self._lock:
            instruction = self._instruction
        if instruction is not None:
            self._exchanges.cancel(instruction.worker_operation_id)

    @staticmethod
    def _require_runtime(
        runtime: CodexSharedRuntime,
        instruction: CodexSelectionInstruction,
    ) -> None:
        authority = runtime.authority
        if (
            authority is None
            or authority.control_socket.device != instruction.socket_device
            or authority.control_socket.inode != instruction.socket_inode
        ):
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_CHANGED)

    def _readback(
        self,
        runtime: CodexSharedRuntime,
        reply: CodexSelectionReply,
        instruction: CodexSelectionInstruction,
    ) -> tuple[
        SidekickAccountId | None,
        AuthorityGeneration | None,
    ]:
        observation = runtime.observe_auth(self._wall_time())
        target = None
        if (
            reply.binding.provider_identity is not None
            and reply.binding.generation is not None
        ):
            target = CodexProjectionExpectation(
                reply.binding.account_id,
                reply.binding.provider_identity,
                reply.binding.generation,
            )
        with self._lock:
            projection = (
                self._readback_projection
                if self._instruction == instruction
                else None
            )
        matched = self._match_observation(
            observation,
            projection,
            target,
            reply.baseline,
        )
        if matched is None:
            return None, None
        return matched.account_id, matched.generation

    def _match_observation(
        self,
        observation: ProviderAuthObservation,
        projection: ProviderAuthObservation | None,
        target: CodexProjectionExpectation | None,
        baseline: CodexProjectionExpectation | None,
    ) -> CodexProjectionExpectation | None:
        if (
            projection is None
            or observation.provider_identity != projection.provider_identity
            or observation.generation != projection.generation
        ):
            return None
        for candidate in (target, baseline):
            if (
                candidate is not None
                and self._saved_authority.matches_expectation(
                    candidate,
                    observation,
                )
            ):
                return candidate
        return None

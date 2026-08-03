"""Epoch-bound Codex selection protocol and resident exchange owner."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from threading import Lock

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    FinalizedSelection,
    ProviderAuthObservation,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.codex.auth.token import (
    decode_codex_token_claims,
)
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.external_auth.codec import (
    decode_worker_message,
    encode_worker_message,
    worker_message_integer,
    worker_message_text,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexDaemonAuthority,
    CodexExchangeDeadlines,
    CodexProjectionExpectation,
    CodexProjectionReceipt,
    CodexProjectionReplyLease,
)
from sidekick_usages.providers.codex.broker.ports import (
    CodexProjection,
    CodexRuntimeStateReader,
    CodexSavedAuthorityRelation,
    CodexWorkerExchange,
    CodexWorkerExchangeFactory,
)
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from sidekick_usages.providers.codex.session.models import CodexRelayAuthority
from sidekick_usages.providers.codex.session.quiescence import (
    CodexParticipantProofError,
    CodexParticipantProofSet,
)
from sidekick_usages.serialization.framing import clear_mutable_buffer
from sidekick_usages.serialization.json import (
    JsonEncodeError,
    JsonObject,
    encode_compact_json_buffer,
)

CODEX_SELECTION_PROTOCOL_VERSION = 1
CODEX_SELECTION_RESPONSE_SECONDS = 90.0
CODEX_SELECTION_COMPLETION_SECONDS = 120.0
CODEX_SELECTION_INSTALL_RESERVE_SECONDS = 0.5
_NANOSECONDS_PER_SECOND = 1_000_000_000
_INSTRUCTION_KEYS = frozenset(
    {
        "account_id",
        "completion_deadline_nanoseconds",
        "kind",
        "operation_id",
        "protocol_version",
        "response_deadline_nanoseconds",
        "socket_device",
        "socket_inode",
        "worker_operation_id",
    }
)
_BINDING_KEYS = {
    "account_id",
    "generation",
    "kind",
    "operation_id",
    "pending_epoch",
    "protocol_version",
    "provider_identity",
    "socket_device",
    "socket_inode",
    "worker_operation_id",
}
_PROOF_REPLY_KEYS = frozenset(_BINDING_KEYS)
_COMMIT_REPLY_KEYS = frozenset(_BINDING_KEYS | {"access_token", "plan"})
_READBACK_REPLY_KEYS = frozenset(
    _BINDING_KEYS
    | {
        "baseline_account_id",
        "baseline_generation",
        "baseline_provider_identity",
    }
)
_ACKNOWLEDGEMENT_KEYS = frozenset(
    _BINDING_KEYS | {"observed_account_id", "observed_generation"}
)


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexSelectionInstruction:
    """Non-secret worker correlation tied to one resident socket."""

    worker_operation_id: OperationId
    operation_id: OperationId
    kind: OperationKind
    account_id: SidekickAccountId
    socket_device: int
    socket_inode: int
    deadlines: CodexExchangeDeadlines

    def __post_init__(self) -> None:
        """Require one exact selection phase and qualified socket."""
        if not self.kind.is_selection_worker:
            raise ValueError("Codex selection instruction kind is invalid.")
        if (
            type(self.socket_device) is not int
            or type(self.socket_inode) is not int
            or min(self.socket_device, self.socket_inode) < 1
        ):
            raise ValueError("Codex selection socket identity is invalid.")


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexSelectionBinding:
    """Complete target authority bound to one selection phase."""

    worker_operation_id: OperationId
    operation_id: OperationId
    kind: OperationKind
    pending_epoch: SelectionEpoch
    account_id: SidekickAccountId
    provider_identity: ProviderIdentity | None
    generation: AuthorityGeneration | None
    socket_device: int
    socket_inode: int

    def __post_init__(self) -> None:
        """Require a complete selection phase and qualified socket."""
        if not self.kind.is_selection_worker:
            raise ValueError("Codex selection binding kind is invalid.")
        if (self.provider_identity is None) != (self.generation is None) or (
            self.kind is not OperationKind.SELECTION_READBACK
            and self.provider_identity is None
        ):
            raise ValueError("Codex selection target authority is incomplete.")
        if (
            type(self.socket_device) is not int
            or type(self.socket_inode) is not int
            or min(self.socket_device, self.socket_inode) < 1
        ):
            raise ValueError("Codex selection socket identity is invalid.")

    def matches(self, instruction: CodexSelectionInstruction) -> bool:
        """Return whether this binding completes the exact instruction."""
        return (
            self.worker_operation_id == instruction.worker_operation_id
            and self.operation_id == instruction.operation_id
            and self.kind is instruction.kind
            and self.account_id == instruction.account_id
            and self.socket_device == instruction.socket_device
            and self.socket_inode == instruction.socket_inode
        )


@dataclass(slots=True)
class CodexSelectionReply:
    """Strict worker reply with phase-owned protected state."""

    binding: CodexSelectionBinding
    projection: CodexProjectionReplyLease | None
    baseline: CodexProjectionExpectation | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexSelectionAcknowledgement:
    """Secret-free resident observation for one exact binding."""

    binding: CodexSelectionBinding
    observed_account_id: SidekickAccountId | None
    observed_generation: AuthorityGeneration | None

    def __post_init__(self) -> None:
        """Require observation identity and generation to be atomic."""
        if (self.observed_account_id is None) != (
            self.observed_generation is None
        ):
            raise ValueError("Codex selection observation is incomplete.")


def encode_codex_selection_instruction(
    instruction: CodexSelectionInstruction,
) -> bytes:
    """Encode one resident-qualified selection instruction."""
    return encode_worker_message(
        {
            "account_id": str(instruction.account_id),
            "completion_deadline_nanoseconds": (
                instruction.deadlines.completion_deadline_nanoseconds
            ),
            "kind": instruction.kind.value,
            "operation_id": str(instruction.operation_id),
            "protocol_version": CODEX_SELECTION_PROTOCOL_VERSION,
            "response_deadline_nanoseconds": (
                instruction.deadlines.response_deadline_nanoseconds
            ),
            "socket_device": instruction.socket_device,
            "socket_inode": instruction.socket_inode,
            "worker_operation_id": str(instruction.worker_operation_id),
        }
    )


def decode_codex_selection_instruction(
    payload: bytes | bytearray,
) -> CodexSelectionInstruction:
    """Decode one exact resident-qualified selection instruction."""
    root = decode_worker_message(
        payload,
        _INSTRUCTION_KEYS,
        CODEX_SELECTION_PROTOCOL_VERSION,
    )
    try:
        return CodexSelectionInstruction(
            worker_operation_id=OperationId(
                worker_message_text(root, "worker_operation_id")
            ),
            operation_id=OperationId(
                worker_message_text(root, "operation_id")
            ),
            kind=OperationKind(worker_message_text(root, "kind")),
            account_id=SidekickAccountId(
                worker_message_text(root, "account_id")
            ),
            socket_device=worker_message_integer(root, "socket_device"),
            socket_inode=worker_message_integer(root, "socket_inode"),
            deadlines=CodexExchangeDeadlines(
                response_deadline_nanoseconds=worker_message_integer(
                    root,
                    "response_deadline_nanoseconds",
                ),
                completion_deadline_nanoseconds=worker_message_integer(
                    root,
                    "completion_deadline_nanoseconds",
                ),
            ),
        )
    except TypeError, ValueError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None


def encode_codex_selection_reply(
    instruction: CodexSelectionInstruction,
    binding: CodexSelectionBinding,
    *,
    projection: CodexProjection | None = None,
    baseline: CodexProjectionExpectation | None = None,
) -> bytearray:
    """Encode one phase-specific target proof or protected projection."""
    if not binding.matches(instruction):
        raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
    values = _binding_values(binding)
    if instruction.kind is OperationKind.SELECTION_COMMIT:
        if (
            projection is None
            or binding.provider_identity is None
            or binding.generation is None
            or baseline is not None
            or projection.account_id != binding.account_id
            or projection.provider_identity != binding.provider_identity
            or projection.generation != binding.generation
        ):
            raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
        values.update(
            {
                "access_token": projection.access_token,
                "plan": projection.plan,
            }
        )
    elif instruction.kind is OperationKind.SELECTION_READBACK:
        if projection is not None:
            raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED)
        values.update(_baseline_values(baseline))
    elif projection is not None or baseline is not None:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED)
    try:
        return encode_compact_json_buffer(values)
    except JsonEncodeError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None


def decode_codex_selection_reply(
    payload: bytes | bytearray,
    instruction: CodexSelectionInstruction,
) -> CodexSelectionReply:
    """Decode one reply and corroborate its complete selection binding."""
    keys = {
        OperationKind.SELECTION_PREVALIDATE: _PROOF_REPLY_KEYS,
        OperationKind.SELECTION_COMMIT: _COMMIT_REPLY_KEYS,
        OperationKind.SELECTION_READBACK: _READBACK_REPLY_KEYS,
    }[instruction.kind]
    root = decode_worker_message(
        payload,
        keys,
        CODEX_SELECTION_PROTOCOL_VERSION,
    )
    binding = _decode_binding(root)
    if not binding.matches(instruction):
        raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
    projection = (
        _decode_projection(root, binding)
        if instruction.kind is OperationKind.SELECTION_COMMIT
        else None
    )
    baseline = (
        _decode_baseline(root)
        if instruction.kind is OperationKind.SELECTION_READBACK
        else None
    )
    return CodexSelectionReply(binding, projection, baseline)


def encode_codex_selection_acknowledgement(
    acknowledgement: CodexSelectionAcknowledgement,
) -> bytes:
    """Encode one exact secret-free resident selection observation."""
    values = _binding_values(acknowledgement.binding)
    values.update(
        {
            "observed_account_id": (
                None
                if acknowledgement.observed_account_id is None
                else str(acknowledgement.observed_account_id)
            ),
            "observed_generation": (
                None
                if acknowledgement.observed_generation is None
                else str(acknowledgement.observed_generation)
            ),
        }
    )
    return encode_worker_message(values)


def decode_codex_selection_acknowledgement(
    payload: bytes | bytearray,
    binding: CodexSelectionBinding,
) -> CodexSelectionAcknowledgement:
    """Decode one acknowledgement for the exact originating binding."""
    root = decode_worker_message(
        payload,
        _ACKNOWLEDGEMENT_KEYS,
        CODEX_SELECTION_PROTOCOL_VERSION,
    )
    received = _decode_binding(root)
    if received != binding:
        raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
    try:
        account_text = _optional_text(root, "observed_account_id")
        generation_text = _optional_text(root, "observed_generation")
        return CodexSelectionAcknowledgement(
            binding=received,
            observed_account_id=(
                None
                if account_text is None
                else SidekickAccountId(account_text)
            ),
            observed_generation=(
                None
                if generation_text is None
                else AuthorityGeneration(generation_text)
            ),
        )
    except TypeError, ValueError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None


def _binding_values(binding: CodexSelectionBinding) -> JsonObject:
    return {
        "account_id": str(binding.account_id),
        "generation": (
            None if binding.generation is None else str(binding.generation)
        ),
        "kind": binding.kind.value,
        "operation_id": str(binding.operation_id),
        "pending_epoch": binding.pending_epoch.value,
        "protocol_version": CODEX_SELECTION_PROTOCOL_VERSION,
        "provider_identity": (
            None
            if binding.provider_identity is None
            else str(binding.provider_identity)
        ),
        "socket_device": binding.socket_device,
        "socket_inode": binding.socket_inode,
        "worker_operation_id": str(binding.worker_operation_id),
    }


def _baseline_values(
    baseline: CodexProjectionExpectation | None,
) -> JsonObject:
    return {
        "baseline_account_id": (
            None if baseline is None else str(baseline.account_id)
        ),
        "baseline_generation": (
            None if baseline is None else str(baseline.generation)
        ),
        "baseline_provider_identity": (
            None if baseline is None else str(baseline.provider_identity)
        ),
    }


def _decode_binding(root: JsonObject) -> CodexSelectionBinding:
    try:
        provider_identity = _optional_text(root, "provider_identity")
        generation = _optional_text(root, "generation")
        return CodexSelectionBinding(
            worker_operation_id=OperationId(
                worker_message_text(root, "worker_operation_id")
            ),
            operation_id=OperationId(
                worker_message_text(root, "operation_id")
            ),
            kind=OperationKind(worker_message_text(root, "kind")),
            pending_epoch=SelectionEpoch(
                worker_message_integer(root, "pending_epoch")
            ),
            account_id=SidekickAccountId(
                worker_message_text(root, "account_id")
            ),
            provider_identity=(
                None
                if provider_identity is None
                else ProviderIdentity(provider_identity)
            ),
            generation=(
                None if generation is None else AuthorityGeneration(generation)
            ),
            socket_device=worker_message_integer(root, "socket_device"),
            socket_inode=worker_message_integer(root, "socket_inode"),
        )
    except TypeError, ValueError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None


def _decode_projection(
    root: JsonObject,
    binding: CodexSelectionBinding,
) -> CodexProjectionReplyLease:
    access_token = worker_message_text(root, "access_token")
    try:
        if binding.provider_identity is None or binding.generation is None:
            raise TypeError
        if (
            decode_codex_token_claims(access_token).provider_identity
            != binding.provider_identity
        ):
            raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
        return CodexProjectionReplyLease(
            operation_id=binding.worker_operation_id,
            account_id=binding.account_id,
            provider_identity=binding.provider_identity,
            generation=binding.generation,
            plan=worker_message_text(root, "plan"),
            access_token=access_token,
        )
    except TypeError, ValueError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None


def _decode_baseline(root: JsonObject) -> CodexProjectionExpectation | None:
    try:
        account = _optional_text(root, "baseline_account_id")
        identity = _optional_text(root, "baseline_provider_identity")
        generation = _optional_text(root, "baseline_generation")
        if account is None and identity is None and generation is None:
            return None
        if account is None or identity is None or generation is None:
            raise TypeError
        return CodexProjectionExpectation(
            SidekickAccountId(account),
            ProviderIdentity(identity),
            AuthorityGeneration(generation),
        )
    except TypeError, ValueError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None


def _optional_text(root: JsonObject, name: str) -> str | None:
    value = root.get(name)
    if value is not None and not isinstance(value, str):
        raise TypeError
    return value


class CodexSelectionBroker:
    """Bind selection workers to the current qualified resident socket."""

    def __init__(
        self,
        exchanges: CodexWorkerExchangeFactory,
        saved_authority: CodexSavedAuthorityRelation,
        runtime_state: CodexRuntimeStateReader,
        participant_proofs: CodexParticipantProofSet,
        *,
        wall_time: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._exchanges = exchanges
        self._saved_authority = saved_authority
        self._runtime_state = runtime_state
        self._participant_proofs = participant_proofs
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
            observed_account_id = reply.binding.account_id
            observed_generation = reply.binding.generation
        elif instruction.kind is OperationKind.SELECTION_COMMIT:
            projection = reply.projection
            if projection is None:
                raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED)
            target = CodexRelayAuthority(
                account_id=projection.account_id,
                generation=projection.generation,
                epoch=reply.binding.pending_epoch,
            )
            try:
                self._participant_proofs.prepare(
                    reply.binding.operation_id,
                    reply.binding.pending_epoch,
                )
            except CodexParticipantProofError:
                raise CodexBrokerError(
                    CodexBrokerFailure.PROTOCOL_UNSUPPORTED
                ) from None
            proof_pending = True
            try:
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
                try:
                    self._participant_proofs.complete(
                        reply.binding.operation_id,
                        target,
                    )
                except CodexParticipantProofError:
                    raise CodexBrokerError(
                        CodexBrokerFailure.PROTOCOL_UNSUPPORTED
                    ) from None
                proof_pending = False
            finally:
                if proof_pending:
                    self._participant_proofs.abort(
                        reply.binding.operation_id,
                        reply.binding.pending_epoch,
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
            if (
                observed_account_id == reply.binding.account_id
                and observed_generation == reply.binding.generation
                and observed_generation is not None
            ):
                try:
                    self._participant_proofs.bind_after_readback(
                        reply.binding.operation_id,
                        CodexRelayAuthority(
                            account_id=reply.binding.account_id,
                            generation=observed_generation,
                            epoch=reply.binding.pending_epoch,
                        ),
                    )
                except CodexParticipantProofError:
                    raise CodexBrokerError(
                        CodexBrokerFailure.PROTOCOL_UNSUPPORTED
                    ) from None
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

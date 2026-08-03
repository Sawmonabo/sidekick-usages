"""Epoch-bound Codex selection exchange protocol."""

from dataclasses import dataclass

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import SelectionEpoch
from sidekick_usages.core.selection.types import OperationKind
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
    CodexExchangeDeadlines,
    CodexProjectionExpectation,
    CodexProjectionReplyLease,
)
from sidekick_usages.providers.codex.broker.ports import CodexProjection
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from sidekick_usages.serialization.json import (
    JsonEncodeError,
    JsonObject,
    encode_compact_json_buffer,
)

CODEX_SELECTION_PROTOCOL_VERSION = 1
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
    provider_identity: ProviderIdentity
    generation: AuthorityGeneration
    socket_device: int
    socket_inode: int

    def __post_init__(self) -> None:
        """Require a complete selection phase and qualified socket."""
        if not self.kind.is_selection_worker:
            raise ValueError("Codex selection binding kind is invalid.")
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
        "generation": str(binding.generation),
        "kind": binding.kind.value,
        "operation_id": str(binding.operation_id),
        "pending_epoch": binding.pending_epoch.value,
        "protocol_version": CODEX_SELECTION_PROTOCOL_VERSION,
        "provider_identity": str(binding.provider_identity),
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
            provider_identity=ProviderIdentity(
                worker_message_text(root, "provider_identity")
            ),
            generation=AuthorityGeneration(
                worker_message_text(root, "generation")
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

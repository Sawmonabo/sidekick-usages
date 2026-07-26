"""Strict activation exchange between worker and resident broker."""

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
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
    CodexActivationAcknowledgement,
    CodexActivationInstruction,
    CodexExchangeDeadlines,
    CodexProjectionReceipt,
    CodexProjectionReplyLease,
)
from sidekick_usages.providers.codex.broker.ports import CodexProjection
from sidekick_usages.providers.codex.broker.types import (
    CodexActivationMode,
    CodexBrokerFailure,
)
from sidekick_usages.serialization.json import (
    JsonEncodeError,
    encode_compact_json_buffer,
)

CODEX_ACTIVATION_PROTOCOL_VERSION = 1
CODEX_ACTIVATION_INSTALLED = "installed"
_INSTRUCTION_KEYS = frozenset(
    {
        "account_id",
        "completion_deadline_nanoseconds",
        "mode",
        "operation_id",
        "protocol_version",
        "response_deadline_nanoseconds",
        "rollback_account_id",
    }
)
_REPLY_KEYS = frozenset(
    {
        "access_token",
        "account_id",
        "generation",
        "mode",
        "operation_id",
        "plan",
        "protocol_version",
        "provider_identity",
    }
)
_ACKNOWLEDGEMENT_KEYS = frozenset(
    {
        "account_id",
        "generation",
        "mode",
        "operation_id",
        "outcome",
        "plan",
        "protocol_version",
        "provider_identity",
        "socket_device",
        "socket_inode",
    }
)


def encode_codex_activation_instruction(
    instruction: CodexActivationInstruction,
) -> bytes:
    """Encode one non-secret activation instruction."""
    return encode_worker_message(
        {
            "account_id": str(instruction.account_id),
            "completion_deadline_nanoseconds": (
                instruction.deadlines.completion_deadline_nanoseconds
            ),
            "mode": instruction.mode.value,
            "operation_id": str(instruction.operation_id),
            "protocol_version": CODEX_ACTIVATION_PROTOCOL_VERSION,
            "response_deadline_nanoseconds": (
                instruction.deadlines.response_deadline_nanoseconds
            ),
            "rollback_account_id": (
                None
                if instruction.rollback_account_id is None
                else str(instruction.rollback_account_id)
            ),
        }
    )


def decode_codex_activation_instruction(
    payload: bytes | bytearray,
) -> CodexActivationInstruction:
    """Decode one exact activation instruction."""
    root = decode_worker_message(
        payload,
        _INSTRUCTION_KEYS,
        CODEX_ACTIVATION_PROTOCOL_VERSION,
    )
    try:
        rollback_value = root.get("rollback_account_id")
        if rollback_value is not None and not isinstance(
            rollback_value,
            str,
        ):
            raise TypeError
        return CodexActivationInstruction(
            operation_id=OperationId(
                worker_message_text(root, "operation_id")
            ),
            mode=CodexActivationMode(worker_message_text(root, "mode")),
            account_id=SidekickAccountId(
                worker_message_text(root, "account_id")
            ),
            rollback_account_id=(
                None
                if rollback_value is None
                else SidekickAccountId(rollback_value)
            ),
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


def encode_codex_activation_reply(
    instruction: CodexActivationInstruction,
    projection: CodexProjection,
) -> bytearray:
    """Encode one identity-bound projection for the resident broker."""
    if not instruction.permits(projection.account_id):
        raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
    try:
        return encode_compact_json_buffer(
            {
                "access_token": projection.access_token,
                "account_id": str(projection.account_id),
                "generation": str(projection.generation),
                "mode": instruction.mode.value,
                "operation_id": str(instruction.operation_id),
                "plan": projection.plan,
                "protocol_version": CODEX_ACTIVATION_PROTOCOL_VERSION,
                "provider_identity": str(projection.provider_identity),
            }
        )
    except JsonEncodeError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None


def decode_codex_activation_reply(
    payload: bytes | bytearray,
    instruction: CodexActivationInstruction,
) -> CodexProjectionReplyLease:
    """Decode and corroborate one worker projection."""
    root = decode_worker_message(
        payload,
        _REPLY_KEYS,
        CODEX_ACTIVATION_PROTOCOL_VERSION,
    )
    access_token = worker_message_text(root, "access_token")
    try:
        operation_id = OperationId(worker_message_text(root, "operation_id"))
        mode = CodexActivationMode(worker_message_text(root, "mode"))
        account_id = SidekickAccountId(worker_message_text(root, "account_id"))
        provider_identity = ProviderIdentity(
            worker_message_text(root, "provider_identity")
        )
        generation = AuthorityGeneration(
            worker_message_text(root, "generation")
        )
        plan = worker_message_text(root, "plan")
        claimed_identity = decode_codex_token_claims(
            access_token
        ).provider_identity
    except TypeError, ValueError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None
    if (
        operation_id != instruction.operation_id
        or mode is not instruction.mode
        or not instruction.permits(account_id)
        or claimed_identity != provider_identity
    ):
        raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
    return CodexProjectionReplyLease(
        operation_id=operation_id,
        account_id=account_id,
        provider_identity=provider_identity,
        generation=generation,
        plan=plan,
        access_token=access_token,
    )


def encode_codex_activation_acknowledgement(
    acknowledgement: CodexActivationAcknowledgement,
) -> bytes:
    """Encode one correlated official-install receipt."""
    receipt = acknowledgement.receipt
    return encode_worker_message(
        {
            "account_id": str(receipt.account_id),
            "generation": str(receipt.generation),
            "mode": acknowledgement.mode.value,
            "operation_id": str(acknowledgement.operation_id),
            "outcome": CODEX_ACTIVATION_INSTALLED,
            "plan": receipt.plan,
            "protocol_version": CODEX_ACTIVATION_PROTOCOL_VERSION,
            "provider_identity": str(receipt.provider_identity),
            "socket_device": receipt.socket_device,
            "socket_inode": receipt.socket_inode,
        }
    )


def decode_codex_activation_acknowledgement(
    payload: bytes | bytearray,
    instruction: CodexActivationInstruction,
    provider_identity: ProviderIdentity,
    generation: AuthorityGeneration,
) -> CodexActivationAcknowledgement:
    """Decode the exact receipt for the originating projection."""
    root = decode_worker_message(
        payload,
        _ACKNOWLEDGEMENT_KEYS,
        CODEX_ACTIVATION_PROTOCOL_VERSION,
    )
    try:
        acknowledgement = CodexActivationAcknowledgement(
            operation_id=OperationId(
                worker_message_text(root, "operation_id")
            ),
            mode=CodexActivationMode(worker_message_text(root, "mode")),
            receipt=CodexProjectionReceipt(
                account_id=SidekickAccountId(
                    worker_message_text(root, "account_id")
                ),
                provider_identity=ProviderIdentity(
                    worker_message_text(root, "provider_identity")
                ),
                generation=AuthorityGeneration(
                    worker_message_text(root, "generation")
                ),
                plan=worker_message_text(root, "plan"),
                socket_device=worker_message_integer(
                    root,
                    "socket_device",
                ),
                socket_inode=worker_message_integer(
                    root,
                    "socket_inode",
                ),
            ),
        )
    except TypeError, ValueError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None
    receipt = acknowledgement.receipt
    if (
        root.get("outcome") != CODEX_ACTIVATION_INSTALLED
        or acknowledgement.operation_id != instruction.operation_id
        or acknowledgement.mode is not instruction.mode
        or not instruction.permits(receipt.account_id)
        or receipt.provider_identity != provider_identity
        or receipt.generation != generation
    ):
        raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
    return acknowledgement

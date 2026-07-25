"""Strict shared-daemon refresh and private-worker exchange protocol."""

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.codex.app_server.jsonrpc.codec import (
    MAX_JSON_RPC_INTEGER,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.models import (
    JsonRpcServerRequest,
)
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.models import (
    CodexCallbackAcknowledgement,
    CodexCallbackInstruction,
    CodexRefreshReplyLease,
    CodexRefreshRequest,
)
from sidekick_usages.providers.codex.broker.ports import CodexProjection
from sidekick_usages.providers.codex.broker.types import (
    CodexBrokerFailure,
    CodexCallbackMode,
)
from sidekick_usages.providers.codex.generation import codex_generation_order
from sidekick_usages.providers.codex.token import decode_codex_token_claims
from sidekick_usages.serialization.json import (
    JsonEncodeError,
    JsonObject,
    decode_json_object,
    encode_compact_json,
    encode_compact_json_buffer,
)

CODEX_CALLBACK_PROTOCOL_VERSION = 1
CODEX_REFRESH_METHOD = "account/chatgptAuthTokens/refresh"
CODEX_REFRESH_REASON = "unauthorized"
CODEX_REFRESH_ERROR_CODE = -32000
CODEX_REFRESH_ERROR_MESSAGE = "external auth refresh unavailable"
CODEX_CALLBACK_DISPATCHED = "dispatched"
_INSTRUCTION_KEYS = frozenset(
    {
        "account_id",
        "completion_deadline_nanoseconds",
        "mode",
        "operation_id",
        "protocol_version",
        "provider_identity",
        "response_deadline_nanoseconds",
        "source_generation",
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
        "source_generation",
    }
)
_ACKNOWLEDGEMENT_KEYS = frozenset(
    {
        "generation",
        "mode",
        "operation_id",
        "outcome",
        "protocol_version",
    }
)


def decode_codex_refresh_request(
    request: JsonRpcServerRequest,
) -> CodexRefreshRequest:
    """Decode the exact release-matched daemon refresh subset."""
    previous = request.params.get("previousAccountId")
    if (
        isinstance(request.request_id, bool)
        or not isinstance(request.request_id, int)
        or request.request_id < 0
        or request.request_id > MAX_JSON_RPC_INTEGER
        or request.method != CODEX_REFRESH_METHOD
        or set(request.params) != {"previousAccountId", "reason"}
        or request.params.get("reason") != CODEX_REFRESH_REASON
        or not isinstance(previous, str)
    ):
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED)
    try:
        identity = ProviderIdentity(previous)
    except TypeError, ValueError:
        raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH) from None
    return CodexRefreshRequest(request.request_id, identity)


def encode_codex_callback_instruction(
    instruction: CodexCallbackInstruction,
) -> bytes:
    """Encode one non-secret worker instruction."""
    return _encode(
        {
            "account_id": str(instruction.account_id),
            "completion_deadline_nanoseconds": (
                instruction.completion_deadline_nanoseconds
            ),
            "mode": instruction.mode.value,
            "operation_id": str(instruction.operation_id),
            "protocol_version": CODEX_CALLBACK_PROTOCOL_VERSION,
            "provider_identity": str(instruction.provider_identity),
            "response_deadline_nanoseconds": (
                instruction.response_deadline_nanoseconds
            ),
            "source_generation": str(instruction.source_generation),
        }
    )


def decode_codex_callback_instruction(
    payload: bytes | bytearray,
) -> CodexCallbackInstruction:
    """Decode one exact non-secret worker instruction."""
    root = _decode(payload, _INSTRUCTION_KEYS)
    try:
        return CodexCallbackInstruction(
            operation_id=OperationId(_text(root, "operation_id")),
            mode=CodexCallbackMode(_text(root, "mode")),
            account_id=SidekickAccountId(_text(root, "account_id")),
            provider_identity=ProviderIdentity(
                _text(root, "provider_identity")
            ),
            source_generation=AuthorityGeneration(
                _text(root, "source_generation")
            ),
            response_deadline_nanoseconds=_integer(
                root,
                "response_deadline_nanoseconds",
            ),
            completion_deadline_nanoseconds=_integer(
                root,
                "completion_deadline_nanoseconds",
            ),
        )
    except TypeError, ValueError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None


def encode_codex_refresh_reply(
    instruction: CodexCallbackInstruction,
    projection: CodexProjection,
) -> bytearray:
    """Encode one correlated credential response for the supervisor."""
    if (
        projection.account_id != instruction.account_id
        or projection.provider_identity != instruction.provider_identity
        or not _generation_matches(
            instruction.mode,
            instruction.source_generation,
            projection.generation,
        )
    ):
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
                "protocol_version": CODEX_CALLBACK_PROTOCOL_VERSION,
                "provider_identity": str(projection.provider_identity),
                "source_generation": str(instruction.source_generation),
            }
        )
    except JsonEncodeError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None


def decode_codex_refresh_reply(
    payload: bytes | bytearray,
    instruction: CodexCallbackInstruction,
) -> CodexRefreshReplyLease:
    """Decode and corroborate one isolated-worker credential response."""
    root = _decode(payload, _REPLY_KEYS)
    access_token = _text(root, "access_token")
    try:
        operation_id = OperationId(_text(root, "operation_id"))
        mode = CodexCallbackMode(_text(root, "mode"))
        account_id = SidekickAccountId(_text(root, "account_id"))
        provider_identity = ProviderIdentity(_text(root, "provider_identity"))
        source_generation = AuthorityGeneration(
            _text(root, "source_generation")
        )
        generation = AuthorityGeneration(_text(root, "generation"))
        plan = _text(root, "plan")
        claimed_identity = decode_codex_token_claims(
            access_token
        ).provider_identity
    except TypeError, ValueError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None
    if (
        operation_id != instruction.operation_id
        or mode is not instruction.mode
        or account_id != instruction.account_id
        or provider_identity != instruction.provider_identity
        or source_generation != instruction.source_generation
        or not _generation_matches(
            mode,
            source_generation,
            generation,
        )
        or claimed_identity != provider_identity
    ):
        raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
    return CodexRefreshReplyLease(
        operation_id=operation_id,
        mode=mode,
        account_id=account_id,
        provider_identity=provider_identity,
        source_generation=source_generation,
        generation=generation,
        plan=plan,
        access_token=access_token,
    )


def encode_codex_callback_acknowledgement(
    acknowledgement: CodexCallbackAcknowledgement,
) -> bytes:
    """Encode one secret-free supervisor dispatch acknowledgement."""
    return _encode(
        {
            "generation": str(acknowledgement.generation),
            "mode": acknowledgement.mode.value,
            "operation_id": str(acknowledgement.operation_id),
            "outcome": CODEX_CALLBACK_DISPATCHED,
            "protocol_version": CODEX_CALLBACK_PROTOCOL_VERSION,
        }
    )


def decode_codex_callback_acknowledgement(
    payload: bytes | bytearray,
    instruction: CodexCallbackInstruction,
    generation: AuthorityGeneration,
) -> CodexCallbackAcknowledgement:
    """Decode one exact acknowledgement for the originating worker."""
    root = _decode(payload, _ACKNOWLEDGEMENT_KEYS)
    try:
        acknowledgement = CodexCallbackAcknowledgement(
            operation_id=OperationId(_text(root, "operation_id")),
            mode=CodexCallbackMode(_text(root, "mode")),
            generation=AuthorityGeneration(_text(root, "generation")),
        )
    except TypeError, ValueError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None
    if (
        root.get("outcome") != CODEX_CALLBACK_DISPATCHED
        or acknowledgement.operation_id != instruction.operation_id
        or acknowledgement.mode is not instruction.mode
        or acknowledgement.generation != generation
    ):
        raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
    return acknowledgement


def codex_refresh_result(reply: CodexRefreshReplyLease) -> JsonObject:
    """Build the exact official refresh result from one active lease."""
    return {
        "accessToken": reply.access_token,
        "chatgptAccountId": str(reply.provider_identity),
        "chatgptPlanType": reply.plan,
    }


def _encode(payload: JsonObject) -> bytes:
    try:
        return encode_compact_json(payload)
    except JsonEncodeError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None


def _decode(
    payload: bytes | bytearray,
    expected_keys: frozenset[str],
) -> JsonObject:
    try:
        root = decode_json_object(payload)
    except InvalidPayloadError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None
    if (
        set(root) != expected_keys
        or root.get("protocol_version") != CODEX_CALLBACK_PROTOCOL_VERSION
    ):
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED)
    return root


def _text(root: JsonObject, name: str) -> str:
    value = root.get(name)
    if not isinstance(value, str):
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED)
    return value


def _integer(root: JsonObject, name: str) -> int:
    value = root.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED)
    return value


def _generation_matches(
    mode: CodexCallbackMode,
    source: AuthorityGeneration,
    candidate: AuthorityGeneration,
) -> bool:
    try:
        source_order = codex_generation_order(str(source))
        candidate_order = codex_generation_order(str(candidate))
    except ValueError:
        return False
    if mode is CodexCallbackMode.REFRESH:
        return candidate_order > source_order
    return candidate_order >= source_order

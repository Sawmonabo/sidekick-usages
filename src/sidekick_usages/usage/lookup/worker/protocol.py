"""Strict stable-ID-only global lookup-worker protocol."""

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.serialization.framing import (
    FramingError,
    encode_bounded_frame,
)
from sidekick_usages.serialization.json import (
    JsonDecodeError,
    JsonEncodeError,
    JsonObject,
    JsonValue,
    decode_integer_json_value,
    encode_compact_json,
)
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupEventKind,
    UsageLookupFailure,
    UsageLookupWorkerEvent,
)
from sidekick_usages.usage.models import FetchFailureKind

USAGE_LOOKUP_PROTOCOL_VERSION = 3
MAX_USAGE_LOOKUP_FRAME_BYTES = 512

_EVENT_KEYS = frozenset(
    {
        "account_id",
        "fetch_failure",
        "failure",
        "kind",
        "provider_id",
        "protocol_version",
    }
)


class UsageLookupProtocolError(ValueError):
    """A lookup-worker frame failed its bounded strict contract."""


def encode_usage_lookup_event(event: UsageLookupWorkerEvent) -> bytearray:
    """Encode one immutable lookup event as a complete bounded frame."""
    root: JsonValue = {
        "account_id": (
            None if event.account_id is None else str(event.account_id)
        ),
        "fetch_failure": (
            None if event.fetch_failure is None else event.fetch_failure.value
        ),
        "failure": None if event.failure is None else event.failure.value,
        "kind": event.kind.value,
        "provider_id": (
            None if event.provider_id is None else event.provider_id.value
        ),
        "protocol_version": USAGE_LOOKUP_PROTOCOL_VERSION,
    }
    try:
        payload = encode_compact_json(root)
        return encode_bounded_frame(payload, MAX_USAGE_LOOKUP_FRAME_BYTES)
    except FramingError, JsonEncodeError:
        raise UsageLookupProtocolError from None


def decode_usage_lookup_event(
    payload: bytes | bytearray,
) -> UsageLookupWorkerEvent:
    """Decode one unframed bounded lookup-worker payload."""
    if not payload or len(payload) > MAX_USAGE_LOOKUP_FRAME_BYTES:
        raise UsageLookupProtocolError
    try:
        root = _require_object(decode_integer_json_value(payload))
        if set(root) != _EVENT_KEYS:
            raise UsageLookupProtocolError
        protocol_version = root["protocol_version"]
        if (
            type(protocol_version) is not int
            or protocol_version != USAGE_LOOKUP_PROTOCOL_VERSION
        ):
            raise UsageLookupProtocolError
        kind = UsageLookupEventKind(_require_string(root["kind"]))
        account_id = _optional_account_id(root["account_id"])
        provider_id = _optional_provider_id(root["provider_id"])
        fetch_failure = _optional_fetch_failure(root["fetch_failure"])
        failure = _optional_failure(root["failure"])
        return UsageLookupWorkerEvent(
            kind=kind,
            account_id=account_id,
            provider_id=provider_id,
            fetch_failure=fetch_failure,
            failure=failure,
        )
    except (
        JsonDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        raise UsageLookupProtocolError from None


def _require_object(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise UsageLookupProtocolError
    return value


def _require_string(value: JsonValue) -> str:
    if not isinstance(value, str):
        raise UsageLookupProtocolError
    return value


def _optional_account_id(value: JsonValue) -> SidekickAccountId | None:
    if value is None:
        return None
    return SidekickAccountId(_require_string(value))


def _optional_provider_id(value: JsonValue) -> ProviderId | None:
    if value is None:
        return None
    return ProviderId(_require_string(value))


def _optional_fetch_failure(
    value: JsonValue,
) -> FetchFailureKind | None:
    if value is None:
        return None
    return FetchFailureKind(_require_string(value))


def _optional_failure(value: JsonValue) -> UsageLookupFailure | None:
    if value is None:
        return None
    return UsageLookupFailure(_require_string(value))

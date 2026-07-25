"""Strict JSON helpers for private worker exchanges."""

from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from sidekick_usages.serialization.json import (
    JsonEncodeError,
    JsonObject,
    decode_json_object,
    encode_compact_json,
)


def encode_worker_message(payload: JsonObject) -> bytes:
    """Encode one bounded worker message without exposing its contents."""
    try:
        return encode_compact_json(payload)
    except JsonEncodeError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None


def decode_worker_message(
    payload: bytes | bytearray,
    expected_keys: frozenset[str],
    protocol_version: int,
) -> JsonObject:
    """Decode one exact-version worker message."""
    try:
        root = decode_json_object(payload)
    except InvalidPayloadError:
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED) from None
    if (
        set(root) != expected_keys
        or root.get("protocol_version") != protocol_version
    ):
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED)
    return root


def worker_message_text(root: JsonObject, name: str) -> str:
    """Return one required text field."""
    value = root.get(name)
    if not isinstance(value, str):
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED)
    return value


def worker_message_integer(root: JsonObject, name: str) -> int:
    """Return one required non-boolean integer field."""
    value = root.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CodexBrokerError(CodexBrokerFailure.PROTOCOL_FAILED)
    return value

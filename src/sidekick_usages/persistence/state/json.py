"""Bounded JSON boundary for persisted non-secret state."""

from sidekick_usages.persistence.errors import (
    DuplicateKeyError,
    InvalidSchemaError,
    MalformedJsonError,
)
from sidekick_usages.persistence.limits import MAX_DOCUMENT_BYTES
from sidekick_usages.persistence.state.validation import (
    validate_non_secret_state,
)
from sidekick_usages.serialization import (
    JsonDecodeCode,
    JsonDecodeError,
    JsonEncodeError,
    JsonObject,
    decode_integer_json_value,
    encode_canonical_json,
    validate_integer_json_value,
)

__all__ = [
    "decode_state_object",
    "encode_state_object",
]


def _validate_limit(maximum: int) -> None:
    if maximum <= 0 or maximum > MAX_DOCUMENT_BYTES:
        raise InvalidSchemaError


def decode_state_object(payload: bytes, maximum: int) -> JsonObject:
    """Decode one bounded strict UTF-8 non-secret JSON object."""
    _validate_limit(maximum)
    if not payload or len(payload) > maximum:
        raise InvalidSchemaError
    try:
        decoded = decode_integer_json_value(payload)
    except JsonDecodeError as error:
        if error.code is JsonDecodeCode.DUPLICATE_KEY:
            raise DuplicateKeyError from None
        raise MalformedJsonError from None
    if not isinstance(decoded, dict):
        raise InvalidSchemaError
    validate_non_secret_state(decoded)
    return decoded


def encode_state_object(root: JsonObject, maximum: int) -> bytes:
    """Encode one bounded canonical non-secret JSON object."""
    _validate_limit(maximum)
    validate_non_secret_state(root)
    try:
        validate_integer_json_value(root)
        payload = encode_canonical_json(root)
    except JsonEncodeError:
        raise InvalidSchemaError from None
    if len(payload) > maximum:
        raise InvalidSchemaError
    return payload

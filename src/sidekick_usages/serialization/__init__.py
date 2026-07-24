"""Strict serialization boundaries and JSON vocabulary."""

from sidekick_usages.serialization.json import (
    JsonDecodeCode,
    JsonDecodeError,
    JsonEncodeError,
    JsonObject,
    JsonScalar,
    JsonValue,
    decode_integer_json_value,
    decode_json_object,
    decode_json_value,
    encode_canonical_json,
    encode_compact_json,
    validate_integer_json_value,
)

__all__ = [
    "JsonDecodeCode",
    "JsonDecodeError",
    "JsonEncodeError",
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "decode_integer_json_value",
    "decode_json_object",
    "decode_json_value",
    "encode_canonical_json",
    "encode_compact_json",
    "validate_integer_json_value",
]

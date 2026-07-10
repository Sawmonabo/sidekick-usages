"""Strict serialization boundaries and JSON vocabulary."""

from sidekick_usages.serialization.json import (
    JsonObject,
    JsonScalar,
    JsonValue,
    decode_json_object,
)

__all__ = [
    "JsonObject",
    "JsonScalar",
    "JsonValue",
    "decode_json_object",
]

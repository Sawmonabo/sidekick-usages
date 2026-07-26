"""Strict field validation for persisted non-secret state."""

from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.serialization.json import JsonObject, JsonValue

type FieldNames = set[str] | frozenset[str]


def require_exact_keys(
    value: JsonObject,
    expected: FieldNames,
) -> None:
    """Require an object to contain exactly the expected fields."""
    if set(value) != expected:
        raise InvalidSchemaError


def require_object(value: JsonValue) -> JsonObject:
    """Require one JSON object."""
    if not isinstance(value, dict):
        raise InvalidSchemaError
    return value


def require_list(value: JsonValue) -> list[JsonValue]:
    """Require one JSON list."""
    if not isinstance(value, list):
        raise InvalidSchemaError
    return value


def require_string(value: JsonValue) -> str:
    """Require one JSON string."""
    if not isinstance(value, str):
        raise InvalidSchemaError
    return value


def require_optional_string(value: JsonValue) -> str | None:
    """Require one JSON string or null."""
    if value is None:
        return None
    return require_string(value)


def require_integer(value: JsonValue) -> int:
    """Require one JSON integer, excluding booleans."""
    if type(value) is not int:
        raise InvalidSchemaError
    return value


def require_number(value: JsonValue) -> int | float:
    """Require one JSON number, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise InvalidSchemaError
    return value


def require_boolean(value: JsonValue) -> bool:
    """Require one JSON boolean."""
    if not isinstance(value, bool):
        raise InvalidSchemaError
    return value


def require_schema_version(value: JsonValue, expected: int) -> None:
    """Require one exact integer schema version."""
    if type(value) is not int or value != expected:
        raise InvalidSchemaError

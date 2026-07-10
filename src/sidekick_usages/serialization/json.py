"""Strict decoding for untrusted JSON-object boundaries."""

import json
from typing import NoReturn

from pydantic import ConfigDict, TypeAdapter

from sidekick_usages.errors import InvalidPayloadError

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_JSON_OBJECT_ADAPTER = TypeAdapter(
    JsonObject,
    config=ConfigDict(strict=True, allow_inf_nan=False),
)


def _object_from_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """Build an object while rejecting duplicate member names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_non_finite_number(_value: str) -> NoReturn:
    """Reject non-standard JSON number constants."""
    raise ValueError


def decode_json_object(payload: bytes) -> JsonObject:
    """Decode an untrusted UTF-8 payload as a strict JSON object.

    :param payload: Complete bounded response body.
    :returns: A recursively validated JSON object.
    :raises InvalidPayloadError: If decoding or validation fails.
    """
    try:
        text = payload.decode("utf-8")
        decoded = json.loads(
            text,
            object_pairs_hook=_object_from_pairs,
            parse_constant=_reject_non_finite_number,
        )
        return _JSON_OBJECT_ADAPTER.validate_python(decoded, strict=True)
    except ValueError, RecursionError:
        pass
    raise InvalidPayloadError

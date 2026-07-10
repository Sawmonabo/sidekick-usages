"""Strict decoding for untrusted JSON boundaries."""

import json
import math
from enum import StrEnum
from typing import NoReturn

from pydantic import ConfigDict, TypeAdapter

from sidekick_usages.errors import InvalidPayloadError

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_JSON_VALUE_ADAPTER = TypeAdapter(
    JsonValue,
    config=ConfigDict(strict=True, allow_inf_nan=False),
)


class JsonDecodeCode(StrEnum):
    """Closed lexical JSON failure categories."""

    DUPLICATE_KEY = "duplicate_key"
    MALFORMED_JSON = "malformed_json"


class JsonDecodeError(ValueError):
    """A safe detailed failure from the strict JSON decoder.

    :param code: Stable lexical failure category.
    """

    def __init__(self, code: JsonDecodeCode) -> None:
        self.code = code
        super().__init__(f"Strict JSON decoding failed: {code}.")


class _RepeatedJsonMemberError(ValueError):
    """Internal duplicate-member signal."""


class _NonStandardJsonNumberError(ValueError):
    """Internal non-finite-number signal."""


def _object_from_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """Build an object while rejecting duplicate member names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _RepeatedJsonMemberError
        result[key] = value
    return result


def _reject_non_finite_number(_value: str) -> NoReturn:
    """Reject non-standard JSON number constants."""
    raise _NonStandardJsonNumberError


def _parse_finite_number(value: str) -> float:
    """Decode a JSON float while rejecting numeric overflow."""
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _NonStandardJsonNumberError
    return parsed


def _decode_json_value_unchecked(payload: bytes) -> JsonValue:
    decoded = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_object_from_pairs,
        parse_constant=_reject_non_finite_number,
        parse_float=_parse_finite_number,
    )
    return _JSON_VALUE_ADAPTER.validate_python(decoded, strict=True)


def decode_json_value(payload: bytes) -> JsonValue:
    """Decode strict UTF-8 JSON with safe lexical failure detail.

    :param payload: Complete bytes already bounded by the caller.
    :returns: A recursively validated JSON value.
    :raises JsonDecodeError: If lexical decoding or validation fails.
    """
    try:
        decoded = _decode_json_value_unchecked(payload)
    except _RepeatedJsonMemberError:
        error = JsonDecodeError(JsonDecodeCode.DUPLICATE_KEY)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _NonStandardJsonNumberError,
        RecursionError,
        ValueError,
    ):
        error = JsonDecodeError(JsonDecodeCode.MALFORMED_JSON)
    else:
        return decoded
    raise error


def decode_json_object(payload: bytes) -> JsonObject:
    """Decode an untrusted UTF-8 payload as a strict JSON object.

    :param payload: Complete bounded response body.
    :returns: A recursively validated JSON object.
    :raises InvalidPayloadError: If decoding or validation fails.
    """
    try:
        decoded = decode_json_value(payload)
    except JsonDecodeError:
        error = InvalidPayloadError()
    else:
        if isinstance(decoded, dict):
            return decoded
        error = InvalidPayloadError()
    raise error

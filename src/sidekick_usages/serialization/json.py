"""Strict decoding and deterministic encoding for JSON boundaries."""

import json
import math
from enum import StrEnum
from typing import NoReturn

from sidekick_usages.errors import InvalidPayloadError

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

_MAX_INTEGER = (1 << 63) - 1


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


class JsonEncodeError(ValueError):
    """A JSON value cannot be encoded safely and deterministically."""


class _RepeatedJsonMemberError(ValueError):
    """Internal duplicate-member signal."""


class _InvalidJsonNumberError(ValueError):
    """Internal unsupported or non-finite-number signal."""


class _InvalidJsonValueError(ValueError):
    """Internal non-JSON-value signal."""


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


def _reject_json_number(_value: str) -> NoReturn:
    """Reject a numeric form excluded by the active boundary."""
    raise _InvalidJsonNumberError


def _parse_finite_number(value: str) -> float:
    """Decode a JSON float while rejecting numeric overflow."""
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _InvalidJsonNumberError
    return parsed


def _parse_bounded_integer(value: str) -> int:
    """Decode one signed 63-bit JSON integer."""
    parsed = int(value)
    if parsed < -_MAX_INTEGER or parsed > _MAX_INTEGER:
        raise _InvalidJsonNumberError
    return parsed


def _validated_json_value(
    value: object,
    *,
    integers_only: bool,
) -> JsonValue:
    """Return a recursively typed JSON value."""
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int):
        if integers_only and (value < -_MAX_INTEGER or value > _MAX_INTEGER):
            raise _InvalidJsonValueError
        return value
    if isinstance(value, float):
        if integers_only or not math.isfinite(value):
            raise _InvalidJsonValueError
        return value
    if isinstance(value, list):
        return [
            _validated_json_value(child, integers_only=integers_only)
            for child in value
        ]
    if isinstance(value, dict):
        result: JsonObject = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise _InvalidJsonValueError
            result[key] = _validated_json_value(
                child,
                integers_only=integers_only,
            )
        return result
    raise _InvalidJsonValueError


def _decode_json_value(payload: bytes, *, integers_only: bool) -> JsonValue:
    parse_float = (
        _reject_json_number if integers_only else _parse_finite_number
    )
    parse_int = _parse_bounded_integer if integers_only else int
    decoded: object = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_object_from_pairs,
        parse_constant=_reject_json_number,
        parse_float=parse_float,
        parse_int=parse_int,
    )
    return _validated_json_value(decoded, integers_only=integers_only)


def _strict_decode(payload: bytes, *, integers_only: bool) -> JsonValue:
    try:
        decoded = _decode_json_value(payload, integers_only=integers_only)
    except _RepeatedJsonMemberError:
        error = JsonDecodeError(JsonDecodeCode.DUPLICATE_KEY)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _InvalidJsonNumberError,
        _InvalidJsonValueError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        error = JsonDecodeError(JsonDecodeCode.MALFORMED_JSON)
    else:
        return decoded
    raise error


def decode_json_value(payload: bytes) -> JsonValue:
    """Decode strict UTF-8 JSON with finite standard numbers.

    :param payload: Complete bytes already bounded by the caller.
    :returns: A recursively validated JSON value.
    :raises JsonDecodeError: If lexical decoding or validation fails.
    """
    return _strict_decode(payload, integers_only=False)


def decode_integer_json_value(payload: bytes) -> JsonValue:
    """Decode strict UTF-8 JSON containing only signed 63-bit integers.

    :param payload: Complete bytes already bounded by the caller.
    :returns: A recursively validated JSON value.
    :raises JsonDecodeError: If lexical decoding or validation fails.
    """
    return _strict_decode(payload, integers_only=True)


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


def validate_integer_json_value(value: JsonValue) -> None:
    """Require recursively integer-only JSON with signed 63-bit bounds."""
    try:
        _validated_json_value(value, integers_only=True)
    except _InvalidJsonValueError:
        raise JsonEncodeError from None


def _encode_json(value: JsonValue, *, canonical: bool) -> bytes:
    try:
        validated = _validated_json_value(value, integers_only=False)
        text = json.dumps(
            validated,
            allow_nan=False,
            ensure_ascii=False,
            indent=2 if canonical else None,
            separators=None if canonical else (",", ":"),
            sort_keys=True,
        )
        if canonical:
            text += "\n"
        return text.encode("utf-8")
    except (
        _InvalidJsonValueError,
        RecursionError,
        TypeError,
        ValueError,
        UnicodeEncodeError,
    ):
        raise JsonEncodeError from None


def encode_canonical_json(value: JsonValue) -> bytes:
    """Encode one sorted, indented JSON value with a final newline."""
    return _encode_json(value, canonical=True)


def encode_compact_json(value: JsonValue) -> bytes:
    """Encode one sorted JSON value without insignificant whitespace."""
    return _encode_json(value, canonical=False)

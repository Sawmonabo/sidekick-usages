"""Behavior tests for the strict JSON-object boundary."""

import pytest

from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.serialization import decode_json_object


def test_object_decoder_accepts_the_recursive_json_vocabulary() -> None:
    """A nested object preserves every supported JSON value kind."""
    payload = b'{"values":["text",1,1.5,true,null,{"child":false}]}'

    assert decode_json_object(payload) == {
        "values": ["text", 1, 1.5, True, None, {"child": False}]
    }


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff",
        '{"encoding":"utf-16"}'.encode("utf-16"),
        b'{"missing":',
        b"[]",
        b'"scalar"',
        b"null",
        b'{"outer":{"key":1,"key":2}}',
        b'{"value":NaN}',
        b'{"value":1e999}',
    ],
    ids=[
        "invalid-utf8",
        "utf16",
        "malformed",
        "list",
        "string",
        "null",
        "duplicate-key",
        "nan",
        "overflowing-number",
    ],
)
def test_object_decoder_rejects_invalid_json_boundaries(
    payload: bytes,
) -> None:
    """Invalid syntax, values, and roots share one safe typed failure."""
    with pytest.raises(InvalidPayloadError):
        decode_json_object(payload)


def test_payload_error_does_not_retain_credentials_or_parser_errors() -> None:
    """Boundary failures expose no input or underlying parser exception."""
    credential = "sk-secret-credential"
    payload = f'{{"token":"{credential}","value":1e999}}'.encode()

    with pytest.raises(InvalidPayloadError) as exc_info:
        decode_json_object(payload)

    error = exc_info.value
    assert str(error) == (
        "HTTP payload is invalid or exceeds its allowed size."
    )
    assert credential not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None

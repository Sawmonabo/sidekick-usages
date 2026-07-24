"""Shared validation for secret-free account values."""

import unicodedata

MAX_METADATA_BYTES = 512
MAX_OPAQUE_BYTES = 4_096


def require_bounded_text(
    value: str,
    *,
    name: str,
    maximum: int,
) -> str:
    """Return nonempty bounded UTF-8 without control characters."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{name} must be valid UTF-8.") from None
    if not encoded or len(encoded) > maximum:
        raise ValueError(f"{name} must be nonempty and bounded.")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"{name} must not contain control characters.")
    return value

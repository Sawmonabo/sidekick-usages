"""Recursive secret guards for persisted non-secret state."""

import re

from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.serialization.json import JsonValue

_FORBIDDEN_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "bearer",
        "client_secret",
        "credential",
        "credentials",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "setup_token_value",
        "token",
        "tokens",
    }
)
_SECRET_VALUE = re.compile(
    r"(?:"
    r"sk-ant-[A-Za-z0-9_-]{16,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\."
    r"[A-Za-z0-9_-]{16,}"
    r")\Z",
    re.ASCII,
)


def validate_non_secret_state(value: JsonValue) -> None:
    """Reject credential-shaped keys or values anywhere in state."""
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in _FORBIDDEN_KEYS:
                raise InvalidSchemaError
            validate_non_secret_state(child)
        return
    if isinstance(value, list):
        for child in value:
            validate_non_secret_state(child)
        return
    if isinstance(value, str) and _SECRET_VALUE.fullmatch(value) is not None:
        raise InvalidSchemaError

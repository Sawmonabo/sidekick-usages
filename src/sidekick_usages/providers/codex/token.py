"""Lightweight validation of identity-bearing Codex token claims."""

import binascii
from base64 import b64decode
from datetime import timedelta

from sidekick_usages.core.accounts.generation import (
    hashed_authority_generation,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
)
from sidekick_usages.core.accounts.validation import require_bounded_text
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.codex.models import CodexTokenClaims
from sidekick_usages.serialization.json import decode_json_object

JWT_PART_COUNT = 3
MAX_CODEX_TOKEN_BYTES = 262_144
MAX_CODEX_TOKEN_METADATA_BYTES = 4_096
MAX_CODEX_TOKEN_PLAN_BYTES = 256
CODEX_REFRESH_MARGIN = timedelta(minutes=10)

_AUTH_CLAIM = "https://api.openai.com/auth"
_GENERATION_PREFIX = "access-token-sha256:"


def validated_codex_token(value: str) -> str:
    """Return one nonempty bounded UTF-8 token."""
    return require_bounded_text(
        value,
        name="Codex access token",
        maximum=MAX_CODEX_TOKEN_BYTES,
    )


def codex_access_token_generation(token: str) -> AuthorityGeneration:
    """Return a one-way stable generation for an effective access token."""
    validated = validated_codex_token(token)
    return hashed_authority_generation(
        validated,
        prefix=_GENERATION_PREFIX,
    )


def decode_codex_token_claims(token: str) -> CodexTokenClaims:
    """Decode bounded JWT metadata without trusting its signature."""
    parts = validated_codex_token(token).split(".")
    if len(parts) != JWT_PART_COUNT:
        raise ValueError("Codex access-token metadata is malformed.")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        root = decode_json_object(
            b64decode(payload, altchars=b"-_", validate=True)
        )
    except binascii.Error, InvalidPayloadError, ValueError:
        raise ValueError("Codex access-token metadata is malformed.") from None
    expiry = root.get("exp")
    if expiry is not None and (type(expiry) is not int or expiry < 0):
        raise ValueError("Codex access-token metadata is malformed.")
    auth = root.get(_AUTH_CLAIM)
    if auth is None:
        return CodexTokenClaims(expiry, None, None)
    if not isinstance(auth, dict):
        raise ValueError("Codex access-token metadata is malformed.")
    account_id = auth.get("chatgpt_account_id")
    plan = auth.get("chatgpt_plan_type")
    if (account_id is not None and not isinstance(account_id, str)) or (
        plan is not None and not isinstance(plan, str)
    ):
        raise ValueError("Codex access-token metadata is malformed.")
    try:
        identity = (
            None
            if account_id is None
            else ProviderIdentity(
                require_bounded_text(
                    account_id,
                    name="Codex account identity",
                    maximum=MAX_CODEX_TOKEN_METADATA_BYTES,
                )
            )
        )
        validated_plan = (
            None
            if plan is None
            else require_bounded_text(
                plan,
                name="Codex plan",
                maximum=MAX_CODEX_TOKEN_PLAN_BYTES,
            )
        )
    except TypeError, ValueError:
        raise ValueError("Codex access-token metadata is malformed.") from None
    return CodexTokenClaims(expiry, identity, validated_plan)

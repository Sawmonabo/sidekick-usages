"""Synthetic managed Codex authentication material."""

import base64
import binascii
import json
from collections.abc import Mapping

from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.serialization.json import decode_json_object

NEXT_AUTH_FILE = "next-auth.json"
_JWT_PARTS = 3


def codex_jwt(account_id: str, generation: str) -> str:
    """Build one deterministic JWT-shaped access credential."""

    def encode(value: Mapping[str, object]) -> str:
        payload = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    claims = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "pro",
            "generation": generation,
        }
    }
    return f"{encode({'alg': 'none'})}.{encode(claims)}.sig"


def managed_auth(provider_identity: str, generation: str) -> bytes:
    """Encode one synthetic official managed-home authority."""
    return json.dumps(
        {
            "auth_mode": "chatgpt",
            "last_refresh": generation,
            "tokens": {
                "access_token": codex_jwt(provider_identity, generation),
                "refresh_token": (
                    f"managed-refresh-{provider_identity}-{generation}"
                ),
                "id_token": f"managed-id-{provider_identity}-{generation}",
                "account_id": provider_identity,
            },
        }
    ).encode()


def codex_token_account_id(token: str) -> str | None:
    """Return the synthetic ChatGPT account claim when present."""
    parts = token.split(".")
    if len(parts) != _JWT_PARTS:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = decode_json_object(base64.urlsafe_b64decode(payload))
    except binascii.Error, InvalidPayloadError, ValueError:
        return None
    auth = decoded.get("https://api.openai.com/auth")
    if not isinstance(auth, dict):
        return None
    account_id = auth.get("chatgpt_account_id")
    return account_id if isinstance(account_id, str) else None

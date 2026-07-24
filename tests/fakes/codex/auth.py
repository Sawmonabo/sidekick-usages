"""Synthetic managed Codex authentication material."""

import base64
import json
from collections.abc import Mapping

NEXT_AUTH_FILE = "next-auth.json"


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

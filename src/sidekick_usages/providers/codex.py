"""Codex CLI provider.

Reads OAuth credentials from ``~/.codex/auth.json`` (the default
plaintext path written by Codex CLI on login). Calls
``https://chatgpt.com/backend-api/codex/usage`` and parses the
``primary_window`` (5h), ``secondary_window`` (7d), and per-model
``additional_rate_limits`` buckets.

Codex access tokens expire roughly hourly, so :meth:`refresh_token`
exchanges the stored refresh_token against
``https://auth.openai.com/oauth/token``. The CLI calls this
automatically when usage requests return 401.

Codex has no analogue to ``claude setup-token``, so
:meth:`run_setup_token` raises :class:`UnsupportedOperationError`.
"""

import json
import re
import time
from pathlib import Path
from typing import Any

from sidekick_usages.errors import AuthError, UnsupportedOperationError
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.base import (
    DetectedCredentials,
    Provider,
)
from sidekick_usages.report import UsageReport, UsageWindow
from sidekick_usages.store import Account

USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
OAUTH_REFRESH_ENDPOINT = "https://auth.openai.com/oauth/token"
USER_AGENT = "codex-cli/0.118.0"

# Codex tokens are opaque JWTs. We don't pattern-match them tightly;
# we just look for something that starts with "eyJ" (JWT header) and
# is reasonably long. Looser than Claude's prefix-based shape check.
TOKEN_RE = re.compile(
    r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\."
    r"[A-Za-z0-9_\-]+"
)


class CodexProvider(Provider):
    """Codex CLI integration."""

    id = "codex"
    display_name = "Codex CLI"
    token_pattern = TOKEN_RE

    def __init__(self) -> None:
        """No state of its own."""

    # -- credential detection --------------------------------------
    def detect_credentials(self) -> DetectedCredentials | None:
        """Read credentials from the local Codex CLI install.

        :return: Detected credentials, or ``None`` when no login
            is found on this machine.
        """
        path = Path.home() / ".codex" / "auth.json"
        if not path.exists():
            return None
        try:
            blob = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        return self._parse_blob(blob)

    @staticmethod
    def _parse_blob(
        blob: dict[str, Any],
    ) -> DetectedCredentials | None:
        """Pull credentials out of a Codex auth.json blob.

        :param blob: Parsed auth.json contents.
        :return: ``DetectedCredentials`` or ``None`` on missing keys.
        """
        tokens = blob.get("tokens") or {}
        access = tokens.get("access_token")
        if not access:
            return None
        return DetectedCredentials(
            access_token=access,
            refresh_token=tokens.get("refresh_token"),
            expires_at=blob.get("last_refresh"),
            plan="unknown",  # Plan comes from the usage response.
        )

    # -- usage fetch -----------------------------------------------
    def fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Hit the Codex usage endpoint and parse the response.

        :param account: Account to query.
        :param http: Shared HTTP client.
        :return: Parsed :class:`UsageReport`.
        """
        data = http.get_json(
            USAGE_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {account.access_token}",
                "User-Agent": USER_AGENT,
            },
        )
        windows: list[UsageWindow] = []

        primary = data.get("primary_window") or {}
        if primary:
            windows.append(
                UsageWindow(
                    name="5h",
                    utilization=float(primary.get("used_percent") or 0),
                    resets_at=primary.get("resets_at"),
                )
            )
        secondary = data.get("secondary_window") or {}
        if secondary:
            windows.append(
                UsageWindow(
                    name="7d",
                    utilization=float(secondary.get("used_percent") or 0),
                    resets_at=secondary.get("resets_at"),
                )
            )
        for extra in data.get("additional_rate_limits") or []:
            label = extra.get("label") or extra.get("model") or "?"
            windows.append(
                UsageWindow(
                    name=str(label),
                    utilization=float(extra.get("used_percent") or 0),
                    resets_at=extra.get("resets_at"),
                )
            )
        return UsageReport(
            windows=windows,
            plan=data.get("plan"),
            raw=data,
        )

    # -- refresh ---------------------------------------------------
    def refresh_token(
        self,
        account: Account,
        http: HttpClient,
    ) -> bool:
        """Exchange a refresh token for a new access token.

        :param account: Account whose access_token to refresh.
            Mutated in-place on success.
        :param http: Shared HTTP client.
        :return: True on success, False if no refresh token is
            available or the exchange failed.
        """
        if not account.refresh_token:
            return False
        try:
            response = http.post_form(
                OAUTH_REFRESH_ENDPOINT,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": account.refresh_token,
                },
            )
        except AuthError:
            return False
        new_token = response.get("access_token")
        if not isinstance(new_token, str):
            return False
        account.access_token = new_token
        new_refresh = response.get("refresh_token")
        if isinstance(new_refresh, str):
            account.refresh_token = new_refresh
        expires_in = response.get("expires_in")
        if isinstance(expires_in, int):
            account.expires_at = int(time.time()) + expires_in
        return True

    # -- setup-token -----------------------------------------------
    def run_setup_token(self) -> str | None:
        """Codex has no long-lived token generator.

        :raises UnsupportedOperationError: Always.
        """
        raise UnsupportedOperationError(
            "Codex CLI doesn't expose a long-lived token generator. "
            "Run `codex login` then `sidekick-usages add codex`."
        )

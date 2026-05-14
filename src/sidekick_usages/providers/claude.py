"""Claude Code provider.

Reads OAuth credentials from the local Claude Code install
(macOS Keychain, ``~/.claude/.credentials.json`` on Linux/WSL, or
Windows Credential Manager / ``%APPDATA%/Claude/...``). Calls
``https://api.anthropic.com/api/oauth/usage`` and parses the
``five_hour`` / ``seven_day`` / ``seven_day_opus`` /
``seven_day_oauth_apps`` buckets.

``setup-token`` runs ``claude setup-token`` and scrapes the printed
token. There is no refresh-token flow — Claude tokens from
``setup-token`` last one year, and tokens from ``claude login``
should be refreshed by re-running ``claude login``.
"""

import json
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Any

from sidekick_usages.http import HttpClient
from sidekick_usages.providers.base import (
    DetectedCredentials,
    Provider,
)
from sidekick_usages.report import UsageReport, UsageWindow
from sidekick_usages.store import Account
from sidekick_usages.token_input import TokenInput

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USER_AGENT = "claude-code/2.0.32"
ANTHROPIC_BETA = "oauth-2025-04-20"

BUCKETS: tuple[tuple[str, str], ...] = (
    ("five_hour", "5h"),
    ("seven_day", "7d"),
    ("seven_day_opus", "7d Opus"),
    ("seven_day_oauth_apps", "7d OAuth"),
)


class ClaudeProvider(Provider):
    """Claude Code integration."""

    id = "claude"
    display_name = "Claude Code"
    token_pattern = re.compile(r"sk-ant-oat01-[A-Za-z0-9_\-]+")

    def __init__(self) -> None:
        """No state of its own; uses injected helpers per call."""

    # -- credential detection --------------------------------------
    def detect_credentials(self) -> DetectedCredentials | None:
        """Read credentials from the local Claude Code install.

        :return: Detected credentials, or ``None`` when no login
            is found on this machine.
        """
        system = platform.system()
        if system == "Darwin":
            return self._from_macos_keychain()
        if system == "Linux":
            return self._from_linux_files()
        if system == "Windows":
            return self._from_windows()
        return None

    def _from_macos_keychain(self) -> DetectedCredentials | None:
        """:return: Credentials from macOS Keychain or None."""
        try:
            # Absolute path: /usr/bin/security has been the stable
            # location since OS X 10.0, and using it avoids
            # PATH-injection (bandit B607).
            result = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    "Claude Code-credentials",
                    "-w",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            return self._parse_blob(json.loads(result.stdout.strip()))
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            json.JSONDecodeError,
        ):
            return None

    def _from_linux_files(self) -> DetectedCredentials | None:
        """:return: Credentials from a Linux/WSL creds file or None."""
        for path in (
            Path.home() / ".claude" / ".credentials.json",
            Path.home() / ".config" / "claude" / ".credentials.json",
        ):
            if not path.exists():
                continue
            try:
                return self._parse_blob(json.loads(path.read_text()))
            except json.JSONDecodeError:
                continue
        return None

    def _from_windows(self) -> DetectedCredentials | None:
        """:return: Credentials from Windows storage or None."""
        appdata = Path(os.environ.get("APPDATA", ""))
        for path in (
            Path.home() / ".claude" / ".credentials.json",
            appdata / "Claude" / ".credentials.json",
        ):
            if not path.exists():
                continue
            try:
                return self._parse_blob(json.loads(path.read_text()))
            except json.JSONDecodeError:
                continue
        try:
            ps_script = (
                "$c = Get-StoredCredential "
                "-Target 'Claude Code-credentials' "
                "-ErrorAction SilentlyContinue; "
                "if ($c) { $c.GetNetworkCredential().Password }"
            )
            # Resolve PowerShell to its absolute path via SystemRoot
            # so we don't rely on PATH (bandit B607). Windows env
            # vars are case-insensitive, so the all-caps form
            # ``SYSTEMROOT`` is the portable spelling (SIM112).
            system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
            powershell_bin = (
                rf"{system_root}\System32"
                r"\WindowsPowerShell\v1.0\powershell.exe"
            )
            result = subprocess.run(
                [powershell_bin, "-NoProfile", "-Command", ps_script],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            out = result.stdout.strip()
            if out:
                return self._parse_blob(json.loads(out))
        except (
            subprocess.SubprocessError,
            json.JSONDecodeError,
            FileNotFoundError,
        ):
            pass
        return None

    @staticmethod
    def _parse_blob(
        blob: dict[str, Any],
    ) -> DetectedCredentials | None:
        """Pull credentials out of a Claude Code creds dict.

        :param blob: Parsed credentials dict.
        :return: ``DetectedCredentials`` or ``None`` on missing keys.
        """
        try:
            oauth = blob["claudeAiOauth"]
            token = oauth["accessToken"]
        except KeyError:
            return None
        return DetectedCredentials(
            access_token=token,
            refresh_token=oauth.get("refreshToken"),
            expires_at=oauth.get("expiresAt"),
            plan=oauth.get("subscriptionType") or "unknown",
        )

    # -- usage fetch -----------------------------------------------
    def fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Hit the OAuth usage endpoint and parse the response.

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
                "anthropic-beta": ANTHROPIC_BETA,
            },
        )
        windows: list[UsageWindow] = []
        for key, label in BUCKETS:
            window = data.get(key)
            if not window:
                continue
            windows.append(
                UsageWindow(
                    name=label,
                    utilization=float(window.get("utilization") or 0),
                    resets_at=window.get("resets_at"),
                )
            )
        return UsageReport(
            windows=windows,
            plan=account.plan,
            raw=data,
        )

    # -- refresh ---------------------------------------------------
    def refresh_token(
        self,
        account: Account,
        http: HttpClient,
    ) -> bool:
        """Claude doesn't expose a refresh endpoint we can call here.

        ``claude login`` and ``claude setup-token`` are the only
        supported ways to get a new token, and both are interactive.
        Return False so the CLI emits the standard "re-login" hint.

        :param account: Account whose token failed (unused).
        :param http: Shared HTTP client (unused).
        :return: Always False.
        """
        return False

    # -- setup-token -----------------------------------------------
    def run_setup_token(self) -> str | None:
        """Run ``claude setup-token`` and scrape the printed token.

        :return: A token, or ``None`` on failure.
        :raises UsageError: When ``claude`` is not on PATH.
        """
        ti = TokenInput(self.token_pattern)
        return ti.run_setup_command(
            ["claude", "setup-token"],
            cli_name=self.display_name,
        )

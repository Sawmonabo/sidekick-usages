"""Claude provider facade and Boolean refresh workflow."""

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import assert_never

from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import InvalidExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    DetectedCredentials,
    UsageReport,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import AuthError, InvalidPayloadError, UsageError
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.claude.credentials import (
    detect_credentials,
    require_claude_credentials,
)
from sidekick_usages.providers.claude.schemas import (
    parse_credentials_blob,
    refresh_expiry,
)
from sidekick_usages.providers.claude.usage import (
    fetch_usage as fetch_claude_usage,
)
from sidekick_usages.serialization import decode_json_object

OAUTH_REFRESH_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_REFRESH_EXPIRES_IN_SECONDS = 31_536_000
DEFAULT_REFRESH_SCOPES: tuple[str, ...] = (
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
)


@dataclass(frozen=True, slots=True)
class SetupTokenSuccess:
    """A Claude setup-token process yielded one validated token."""

    token: str
    output_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SetupTokenMissing:
    """Claude setup-token exited without a recognizable token."""

    output_lines: tuple[str, ...]
    return_code: int


@dataclass(frozen=True, slots=True)
class SetupTokenTimedOut:
    """Claude setup-token exceeded its bounded execution time."""


type SetupTokenCapture = (
    SetupTokenSuccess | SetupTokenMissing | SetupTokenTimedOut
)


class ClaudeProvider(Provider):
    """Claude Code provider facade."""

    id = ProviderId.CLAUDE
    display_name = "Claude Code"
    token_pattern = re.compile(r"sk-ant-oat01-[A-Za-z0-9_\-]+")

    def __init__(self, clock: Clock) -> None:
        """Use an injected wall clock for refresh expiry."""
        self.clock = clock

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> DetectedCredentials | None:
        """Read credentials from the local Claude Code install."""
        return detect_credentials(credential_home)

    def fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Fetch usage through Claude's scope-selected route."""
        return fetch_claude_usage(account, http)

    def refresh_token(
        self,
        account: Account,
        http: HttpClient,
    ) -> bool:
        """Refresh one saved Claude account using the current contract."""
        credentials = require_claude_credentials(account)
        if not credentials.refresh_token:
            return False
        if self._refresh_via_cli(account):
            return True
        return self._refresh_via_http(account, http)

    def _refresh_via_cli(self, account: Account) -> bool:
        credentials = require_claude_credentials(account)
        if not credentials.refresh_token:
            return False
        claude_bin = shutil.which("claude")
        if claude_bin is None:
            return False
        scopes = self._refresh_scopes(account)
        with tempfile.TemporaryDirectory(
            prefix="sidekick-claude-refresh-"
        ) as temp_home:
            env = os.environ.copy()
            env["HOME"] = temp_home
            env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] = credentials.refresh_token
            env["CLAUDE_CODE_OAUTH_SCOPES"] = " ".join(scopes)
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            env.pop("ANTHROPIC_API_KEY", None)
            try:
                result = subprocess.run(
                    [claude_bin, "auth", "login", "--claudeai"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except FileNotFoundError, subprocess.SubprocessError:
                return False

            credentials_path = (
                Path(temp_home) / ".claude" / ".credentials.json"
            )
            if result.returncode != 0 and not credentials_path.exists():
                detail = self._redact_tokens(
                    (result.stderr or result.stdout).strip()
                )
                if not detail:
                    detail = f"exit code {result.returncode}"
                raise AuthError(f"Claude CLI refresh failed: {detail}")
            try:
                detected = parse_credentials_blob(
                    decode_json_object(credentials_path.read_bytes())
                )
            except OSError, InvalidPayloadError:
                return False
            if detected is None:
                return False
            if not isinstance(
                detected.credentials,
                ClaudeCredentials,
            ) or isinstance(detected.credentials.expiry, InvalidExpiry):
                raise InvalidPayloadError
            account.credentials = replace(
                credentials,
                access_token=detected.credentials.access_token,
                refresh_token=detected.credentials.refresh_token,
                expiry=detected.credentials.expiry,
                scopes=(
                    detected.credentials.scopes
                    if detected.credentials.scopes is not None
                    else credentials.scopes
                ),
            )
            if detected.plan != "unknown":
                account.plan = detected.plan
            return True

    def _redact_tokens(self, text: str) -> str:
        return self.token_pattern.sub("[redacted]", text)

    def _refresh_via_http(
        self,
        account: Account,
        http: HttpClient,
    ) -> bool:
        credentials = require_claude_credentials(account)
        try:
            scopes = self._refresh_scopes(account)
            response = http.post_json(
                OAUTH_REFRESH_ENDPOINT,
                json_body={
                    "grant_type": "refresh_token",
                    "refresh_token": credentials.refresh_token,
                    "client_id": os.environ.get(
                        "CLAUDE_CODE_OAUTH_CLIENT_ID",
                        OAUTH_CLIENT_ID,
                    ),
                    "scope": " ".join(scopes),
                    "expires_in": OAUTH_REFRESH_EXPIRES_IN_SECONDS,
                },
                operation=HttpOperation.CLAUDE_REFRESH,
            )
        except AuthError:
            return False
        new_token = response.get("access_token")
        if not isinstance(new_token, str) or not new_token:
            return False
        new_refresh = response.get("refresh_token")
        if "refresh_token" in response and (
            not isinstance(new_refresh, str) or not new_refresh
        ):
            raise InvalidPayloadError
        expires_in = response.get("expires_in")
        expiry = UnknownExpiry()
        if expires_in is not None:
            expiry = refresh_expiry(expires_in, self.clock.now())
        account.credentials = replace(
            credentials,
            access_token=new_token,
            refresh_token=(
                new_refresh
                if isinstance(new_refresh, str)
                else credentials.refresh_token
            ),
            expiry=expiry,
        )
        return True

    @staticmethod
    def _refresh_scopes(account: Account) -> tuple[str, ...]:
        scopes = require_claude_credentials(account).scopes
        return DEFAULT_REFRESH_SCOPES if scopes is None else scopes

    def run_setup_token(self) -> str | None:
        """Run ``claude setup-token`` and return only captured token data."""
        result = self.capture_setup_token()
        if isinstance(result, SetupTokenSuccess):
            return result.token
        if isinstance(result, SetupTokenMissing | SetupTokenTimedOut):
            return None
        assert_never(result)

    def capture_setup_token(self, timeout: int = 600) -> SetupTokenCapture:
        """Run Claude's token generator without terminal presentation."""
        resolved = shutil.which("claude")
        if resolved is None:
            raise UsageError(
                "The `claude` CLI is not on PATH. Install Claude Code "
                "first, then re-run this command."
            )
        command = [resolved, "setup-token"]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return SetupTokenTimedOut()
        except FileNotFoundError as error:
            raise UsageError("The `claude` CLI is not on PATH.") from error

        combined = (completed.stdout or "") + "\n" + (completed.stderr or "")
        match = self.token_pattern.search(combined)
        output_lines = tuple(
            line
            for line in combined.splitlines()
            if self.token_pattern.search(line) is None
        )
        if match is None:
            return SetupTokenMissing(output_lines, completed.returncode)
        return SetupTokenSuccess(match.group(0), output_lines)


__all__ = [
    "ClaudeProvider",
    "SetupTokenCapture",
    "SetupTokenMissing",
    "SetupTokenSuccess",
    "SetupTokenTimedOut",
]

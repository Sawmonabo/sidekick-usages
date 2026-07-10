"""Claude Code provider.

Reads OAuth credentials from the local Claude Code install
(macOS Keychain, ``~/.claude/.credentials.json`` on Linux/WSL, or
Windows Credential Manager / ``%APPDATA%/Claude/...``). Calls
``https://api.anthropic.com/api/oauth/usage`` and parses the
``five_hour`` / ``seven_day`` / ``seven_day_opus`` /
``seven_day_oauth_apps`` buckets.

``setup-token`` runs ``claude setup-token`` and scrapes the printed
token. Claude login tokens include refresh tokens and can be renewed
through Claude Code's OAuth token endpoint; setup-token outputs do not
carry refresh tokens and must be replaced manually when rejected.
"""

import os
import platform
import re
import shutil
import subprocess
import tempfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import (
    Expiry,
    InvalidExpiry,
    KnownExpiry,
    UnknownExpiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    DetectedCredentials,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import AuthError, InvalidPayloadError, UsageError
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.providers.base import Provider
from sidekick_usages.serialization import (
    JsonObject,
    JsonValue,
    decode_json_object,
)
from sidekick_usages.token_input import TokenInput

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
OAUTH_REFRESH_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_REFRESH_EXPIRES_IN_SECONDS = 31_536_000
USER_AGENT = "claude-code/2.1.174"
ANTHROPIC_BETA = "oauth-2025-04-20"
ANTHROPIC_API_VERSION = "2023-06-01"

#: Scope on the OAuth token that unlocks ``/api/oauth/usage``.
#: Tokens minted by ``claude setup-token`` lack this scope by design
#: (long-lived tokens are inference-only); for those, fetch_usage
#: routes to :meth:`_fetch_via_headers` instead.
PROFILE_SCOPE = "user:profile"
DEFAULT_REFRESH_SCOPES: tuple[str, ...] = (
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
)

#: Smallest / cheapest model usable for the header probe. ~2 tokens
#: per call (1 input "quota" + 1 max-output). The model is only
#: there to make ``/v1/messages`` a valid request — we discard the
#: completion and read only the response headers.
PROBE_MODEL = "claude-haiku-4-5-20251001"

#: OAuth-endpoint response keys + render labels (full-scope path).
BUCKETS: tuple[tuple[str, str], ...] = (
    ("five_hour", "5h"),
    ("seven_day", "7d"),
    ("seven_day_opus", "7d Opus"),
    ("seven_day_oauth_apps", "7d OAuth"),
)

#: Response-header prefixes + render labels (inference-only path).
#: Anthropic also returns an ``-overage-*`` bucket on the same
#: response; we skip it to match the login-token path, which never
#: surfaces overage as a separate bucket either.
HEADER_BUCKETS: tuple[tuple[str, str], ...] = (
    ("anthropic-ratelimit-unified-5h", "5h"),
    ("anthropic-ratelimit-unified-7d", "7d"),
)


class ClaudeProvider(Provider):
    """Claude Code integration."""

    id = ProviderId.CLAUDE
    display_name = "Claude Code"
    token_pattern = re.compile(r"sk-ant-oat01-[A-Za-z0-9_\-]+")

    def __init__(self, clock: Clock) -> None:
        """Use an injected wall clock for refresh expiry."""
        self.clock = clock

    # -- credential detection --------------------------------------
    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> DetectedCredentials | None:
        """Read credentials from the local Claude Code install.

        :param credential_home: Ignored; Claude Code does not expose
            a CODEX_HOME-style account state directory.
        :return: Detected credentials, or ``None`` when no login
            is found on this machine.
        """
        del credential_home
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
            return self._parse_blob(
                decode_json_object(result.stdout.strip().encode("utf-8"))
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            InvalidPayloadError,
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
                return self._parse_blob(decode_json_object(path.read_bytes()))
            except InvalidPayloadError:
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
                return self._parse_blob(decode_json_object(path.read_bytes()))
            except InvalidPayloadError:
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
                return self._parse_blob(
                    decode_json_object(out.encode("utf-8"))
                )
        except (
            subprocess.SubprocessError,
            InvalidPayloadError,
            FileNotFoundError,
        ):
            pass
        return None

    @staticmethod
    def _parse_blob(
        blob: JsonObject,
    ) -> DetectedCredentials | None:
        """Pull credentials out of a Claude Code creds dict.

        :param blob: Parsed credentials dict.
        :return: ``DetectedCredentials`` or ``None`` on missing keys.
        """
        oauth = blob.get("claudeAiOauth")
        if not isinstance(oauth, dict):
            return None
        token = oauth.get("accessToken")
        if not isinstance(token, str) or not token:
            return None
        raw_scopes = oauth.get("scopes")
        # Tolerate older creds that omit ``scopes`` or store junk in
        # it — only trust a real ``list[str]``, otherwise leave None
        # so the CLI's gate falls back to "attempt and learn from
        # 403". Build via comprehension so the type narrows from
        # ``list[object]`` to ``list[str]`` for the static checker.
        scopes: tuple[str, ...] | None
        if isinstance(raw_scopes, list) and all(
            isinstance(s, str) for s in raw_scopes
        ):
            scopes = tuple(s for s in raw_scopes if isinstance(s, str))
        else:
            scopes = None
        refresh = oauth.get("refreshToken")
        plan = oauth.get("subscriptionType")
        return DetectedCredentials(
            credentials=ClaudeCredentials(
                access_token=token,
                refresh_token=refresh if isinstance(refresh, str) else None,
                expiry=_claude_expiry(oauth.get("expiresAt")),
                scopes=scopes,
            ),
            plan=plan if isinstance(plan, str) and plan else "unknown",
        )

    # -- usage fetch -----------------------------------------------
    def fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Fetch usage windows for one Claude account.

        Two paths converge here. Routing mirrors Claude Code's own
        binary: ``hT()`` (full-scope) calls the OAuth usage endpoint;
        ``UgK()`` (inference-only) probes ``/v1/messages`` and reads
        the unified rate-limit response headers.

        :param account: Account to query.
        :param http: Shared HTTP client.
        :return: Parsed :class:`UsageReport`.
        """
        credentials = _claude_credentials(account)
        if (
            credentials.scopes is not None
            and PROFILE_SCOPE not in credentials.scopes
        ):
            return self._fetch_via_headers(account, http)
        return self._fetch_via_oauth_endpoint(account, http)

    def _fetch_via_oauth_endpoint(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Hit ``/api/oauth/usage`` (requires ``user:profile``).

        :param account: Account to query.
        :param http: Shared HTTP client.
        :return: Parsed :class:`UsageReport` with up to four buckets.
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
            if not isinstance(window, dict) or not window:
                continue
            utilization_value = window.get("utilization")
            utilization = (
                float(utilization_value)
                if isinstance(utilization_value, int | float | str)
                else 0.0
            )
            resets_at = _provider_time(window.get("resets_at"))
            windows.append(
                UsageWindow(
                    name=label,
                    utilization=utilization,
                    resets_at=resets_at,
                )
            )
        return UsageReport(
            windows=tuple(windows),
            plan=account.plan,
        )

    def _fetch_via_headers(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Probe ``/v1/messages`` and parse unified rate-limit headers.

        For inference-only tokens (``claude setup-token`` outputs).
        Mirrors Claude Code's startup probe (``de1()`` / ``UgK()``):
        a ~2-token POST whose response headers carry the
        ``anthropic-ratelimit-unified-{5h,7d}-{utilization,reset}``
        values that drive the binary's in-memory usage UI. The
        completion body is discarded.

        :param account: Account to query.
        :param http: Shared HTTP client.
        :return: Parsed :class:`UsageReport` with 5h and 7d windows.
        """
        response_headers = http.post_capture_headers(
            MESSAGES_URL,
            {
                "model": PROBE_MODEL,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "quota"}],
            },
            {
                "Authorization": f"Bearer {account.access_token}",
                "anthropic-version": ANTHROPIC_API_VERSION,
                "anthropic-beta": ANTHROPIC_BETA,
                "User-Agent": USER_AGENT,
            },
            operation=HttpOperation.CLAUDE_PROBE,
        )
        windows: list[UsageWindow] = []
        for prefix, label in HEADER_BUCKETS:
            window = self._parse_header_window(prefix, label, response_headers)
            if window is not None:
                windows.append(window)
        return UsageReport(
            windows=tuple(windows),
            plan=account.plan,
        )

    @staticmethod
    def _parse_header_window(
        prefix: str,
        label: str,
        response_headers: dict[str, str],
    ) -> UsageWindow | None:
        """Build one :class:`UsageWindow` from header pair, or None.

        :param prefix: Header-name prefix, e.g.
            ``"anthropic-ratelimit-unified-5h"``.
        :param label: Display label (``"5h"`` / ``"7d"``).
        :param response_headers: Lowercase-keyed response headers.
        :return: A window, or ``None`` when either header is absent
            or non-numeric (defensive: header schema is undocumented
            and could drift).
        """
        util_raw = response_headers.get(f"{prefix}-utilization")
        reset_raw = response_headers.get(f"{prefix}-reset")
        if util_raw is None or reset_raw is None:
            return None
        try:
            utilization = float(util_raw) * 100
            reset_unix = int(float(reset_raw))
        except TypeError, ValueError:
            return None
        resets_at = datetime.fromtimestamp(reset_unix, tz=UTC)
        return UsageWindow(
            name=label,
            utilization=utilization,
            resets_at=resets_at,
        )

    # -- refresh ---------------------------------------------------
    def refresh_token(
        self,
        account: Account,
        http: HttpClient,
    ) -> bool:
        """Exchange a Claude OAuth refresh token for a new access token.

        :param account: Account whose token failed. Mutated in-place
            on success.
        :param http: Shared HTTP client.
        :return: True on success, False if no refresh token is
            available, the refresh is rejected, or the response is
            unusable.
        """
        credentials = _claude_credentials(account)
        if not credentials.refresh_token:
            return False
        if self._refresh_via_cli(account):
            return True
        return self._refresh_via_http(account, http)

    def _refresh_via_cli(self, account: Account) -> bool:
        """Ask Claude Code to refresh in an isolated temporary home."""
        credentials = _claude_credentials(account)
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

            creds_path = Path(temp_home) / ".claude" / ".credentials.json"
            if result.returncode != 0 and not creds_path.exists():
                detail = self._redact_tokens(
                    (result.stderr or result.stdout).strip()
                )
                if not detail:
                    detail = f"exit code {result.returncode}"
                raise AuthError(f"Claude CLI refresh failed: {detail}")
            try:
                detected = self._parse_blob(
                    decode_json_object(creds_path.read_bytes())
                )
            except OSError, InvalidPayloadError:
                return False
            if detected is None:
                return False
            if not isinstance(
                detected.credentials, ClaudeCredentials
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
        """Remove Claude OAuth token values from captured CLI output."""
        return self.token_pattern.sub("[redacted]", text)

    def _refresh_via_http(
        self,
        account: Account,
        http: HttpClient,
    ) -> bool:
        """Fallback direct token exchange when Claude Code is unavailable."""
        credentials = _claude_credentials(account)
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
        if not isinstance(new_token, str):
            return False
        new_refresh = response.get("refresh_token")
        expires_in = response.get("expires_in")
        expiry = credentials.expiry
        if expires_in is not None:
            if (
                isinstance(expires_in, bool)
                or not isinstance(expires_in, int)
                or expires_in < 0
            ):
                raise InvalidPayloadError
            reference_time = self.clock.now().astimezone(UTC)
            reference_time = reference_time.replace(
                microsecond=(reference_time.microsecond // 1000) * 1000
            )
            expiry = KnownExpiry(
                reference_time + timedelta(seconds=expires_in)
            )
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
        """Return saved scopes or Claude Code's default OAuth scope set."""
        scopes = _claude_credentials(account).scopes
        return scopes if scopes else DEFAULT_REFRESH_SCOPES

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


def _claude_credentials(account: Account) -> ClaudeCredentials:
    """Return Claude credentials or reject an incompatible account."""
    credentials = account.credentials
    if isinstance(credentials, ClaudeCredentials):
        return credentials
    raise UsageError(f"Account {account.label!r} is not a Claude account.")


def _claude_expiry(value: JsonValue | None) -> Expiry:
    """Normalize Claude's native millisecond expiry metadata."""
    if value is None:
        return UnknownExpiry()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return InvalidExpiry()
    try:
        return KnownExpiry(
            datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)
        )
    except OverflowError:
        return InvalidExpiry()


def _provider_time(value: JsonValue | None) -> datetime | None:
    """Normalize one optional Claude response timestamp."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)

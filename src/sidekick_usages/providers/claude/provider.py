"""Claude provider facade and typed refresh workflow."""

import os
import platform
import shutil
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, Thread
from typing import IO, Protocol

from sidekick_usages.clock import Clock
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    DetectedCredentials,
    UsageReport,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import AuthError
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
    RefreshSuccess,
)
from sidekick_usages.providers.claude.credentials import (
    detect_credentials,
    read_credentials_path,
    require_claude_credentials,
)
from sidekick_usages.providers.claude.schemas import (
    CLAUDE_TOKEN_PATTERN,
    claude_failure,
    parse_refresh_credentials,
    validate_setup_token,
)
from sidekick_usages.providers.claude.usage import (
    fetch_usage as fetch_claude_usage,
)

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
_MAX_SETUP_OUTPUT_BYTES = 1024 * 1024
_SETUP_READ_CHUNK_BYTES = 8192


@dataclass(frozen=True, slots=True)
class SetupTokenSuccess:
    """A Claude setup-token process yielded one validated token."""

    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class SetupTokenMissing:
    """Claude setup-token completed without a recognizable token."""


@dataclass(frozen=True, slots=True)
class SetupTokenRejected:
    """Claude setup-token exited unsuccessfully."""

    return_code: int


@dataclass(frozen=True, slots=True)
class SetupTokenTimedOut:
    """Claude setup-token exceeded its bounded execution time."""


@dataclass(frozen=True, slots=True)
class SetupTokenUnreadable:
    """Claude setup-token could not produce bounded safe output."""


@dataclass(frozen=True, slots=True)
class _CapturedSetupOutput:
    """One bounded private process result awaiting token extraction."""

    return_code: int
    output: bytes = field(repr=False)


type SetupTokenCapture = (
    SetupTokenSuccess
    | SetupTokenMissing
    | SetupTokenRejected
    | SetupTokenTimedOut
    | SetupTokenUnreadable
)
type _SetupProcessResult = (
    _CapturedSetupOutput | SetupTokenTimedOut | SetupTokenUnreadable
)


class ClaudeSetupToken(Protocol):
    """Narrow structural capability for Claude setup-token capture."""

    def capture_setup_token(self, timeout: int = 600) -> SetupTokenCapture:
        """Capture one typed Claude setup-token outcome."""


class ClaudeProvider(Provider):
    """Claude Code provider facade."""

    id = ProviderId.CLAUDE
    display_name = "Claude Code"
    token_pattern = CLAUDE_TOKEN_PATTERN

    def __init__(self, clock: Clock) -> None:
        """Use an injected wall clock for credential expiry."""
        self.clock = clock

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        """Read credentials from the local Claude Code install."""
        return detect_credentials(self.clock.now(), credential_home)

    def credentials_from_token(self, token: str) -> CredentialDetection:
        """Validate one manually supplied Claude OAuth token."""
        if not token:
            return claude_failure(
                ProviderFailureKind.INCOMPLETE,
                "A Claude OAuth token is required.",
            )
        try:
            validated = validate_setup_token(token)
        except ProviderBoundaryError:
            return claude_failure(
                ProviderFailureKind.MALFORMED,
                "The supplied Claude OAuth token is invalid.",
            )
        return DetectedCredentials(
            credentials=ClaudeCredentials(access_token=validated)
        )

    def fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Fetch usage through Claude's scope-selected route."""
        return fetch_claude_usage(account, http)

    def refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        """Return validated replacement credentials without mutation."""
        credentials = require_claude_credentials(account)
        if credentials.refresh_token is None:
            return claude_failure(
                ProviderFailureKind.MISSING,
                "Claude refresh credentials are missing. Log in again.",
            )
        cli_result = self._refresh_via_cli(account)
        if cli_result is not None:
            return cli_result
        return self._refresh_via_http(account, http)

    def _refresh_via_cli(self, account: Account) -> RefreshResult | None:
        credentials = require_claude_credentials(account)
        if credentials.refresh_token is None:
            raise ProviderBoundaryError(
                claude_failure(
                    ProviderFailureKind.MISSING,
                    "Claude refresh credentials are missing. Log in again.",
                )
            ) from None
        claude_bin = (
            None if platform.system() == "Darwin" else shutil.which("claude")
        )
        if claude_bin is None:
            return None
        scopes = self._refresh_scopes(account)
        with tempfile.TemporaryDirectory(
            prefix="sidekick-claude-refresh-"
        ) as temp_home:
            isolated_home = Path(temp_home)
            env = os.environ.copy()
            env["HOME"] = temp_home
            env["USERPROFILE"] = temp_home
            env["APPDATA"] = str(isolated_home / "AppData" / "Roaming")
            env["LOCALAPPDATA"] = str(isolated_home / "AppData" / "Local")
            env["XDG_CONFIG_HOME"] = str(isolated_home / ".config")
            env["CLAUDE_CONFIG_DIR"] = str(isolated_home / ".claude")
            env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] = credentials.refresh_token
            env["CLAUDE_CODE_OAUTH_SCOPES"] = " ".join(scopes)
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            env.pop("ANTHROPIC_API_KEY", None)
            completed = self._run_cli_refresh(
                claude_bin,
                env,
                isolated_home,
            )
            if completed is None or isinstance(completed, ProviderFailure):
                return completed

            credentials_path = (
                Path(env["CLAUDE_CONFIG_DIR"]) / ".credentials.json"
            )
            if completed.returncode != 0 and not credentials_path.exists():
                return claude_failure(
                    ProviderFailureKind.REJECTED,
                    "Claude rejected the credential refresh. Log in again.",
                )
            detected = read_credentials_path(
                credentials_path,
                self.clock.now(),
            )
            if isinstance(detected, ProviderFailure):
                if detected.kind in {
                    ProviderFailureKind.INCOMPLETE,
                    ProviderFailureKind.MALFORMED,
                }:
                    raise ProviderBoundaryError(detected) from None
                if detected.kind is ProviderFailureKind.MISSING:
                    return claude_failure(
                        ProviderFailureKind.INCOMPLETE,
                        "Claude CLI refresh produced no credentials.",
                    )
                return detected
            return self._cli_refresh_success(credentials, detected)

    @staticmethod
    def _run_cli_refresh(
        claude_bin: str,
        env: dict[str, str],
        working_directory: Path,
    ) -> subprocess.CompletedProcess[bytes] | ProviderFailure | None:
        try:
            return subprocess.run(
                [claude_bin, "auth", "login", "--claudeai"],
                env=env,
                cwd=working_directory,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=60,
                check=False,
            )
        except FileNotFoundError:
            return None
        except OSError, subprocess.SubprocessError:
            return claude_failure(
                ProviderFailureKind.UNREADABLE,
                "Claude CLI refresh could not be completed.",
            )

    @staticmethod
    def _cli_refresh_success(
        previous: ClaudeCredentials,
        detected: DetectedCredentials,
    ) -> RefreshSuccess:
        refreshed = detected.credentials
        if not isinstance(refreshed, ClaudeCredentials):
            raise ProviderBoundaryError(
                claude_failure(
                    ProviderFailureKind.IDENTITY_MISMATCH,
                    "Claude refresh returned incompatible credentials.",
                )
            ) from None
        credentials = replace(
            previous,
            access_token=refreshed.access_token,
            refresh_token=(
                refreshed.refresh_token
                if refreshed.refresh_token is not None
                else previous.refresh_token
            ),
            expiry=refreshed.expiry,
            scopes=(
                refreshed.scopes
                if refreshed.scopes is not None
                else previous.scopes
            ),
        )
        return RefreshSuccess(
            credentials=credentials,
            plan=None if detected.plan == "unknown" else detected.plan,
        )

    def _refresh_via_http(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        credentials = require_claude_credentials(account)
        if credentials.refresh_token is None:
            return claude_failure(
                ProviderFailureKind.MISSING,
                "Claude refresh credentials are missing. Log in again.",
            )
        scopes = self._refresh_scopes(account)
        try:
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
            return claude_failure(
                ProviderFailureKind.REJECTED,
                "Claude rejected the credential refresh. Log in again.",
            )
        reference_time = self.clock.now() if "expires_in" in response else None
        refreshed = parse_refresh_credentials(
            response,
            credentials,
            reference_time,
        )
        return RefreshSuccess(credentials=refreshed)

    @staticmethod
    def _refresh_scopes(account: Account) -> tuple[str, ...]:
        scopes = require_claude_credentials(account).scopes
        return DEFAULT_REFRESH_SCOPES if scopes is None else scopes

    def capture_setup_token(self, timeout: int = 600) -> SetupTokenCapture:
        """Run Claude's token generator and return only structured state."""
        resolved = shutil.which("claude")
        if resolved is None:
            completed: _SetupProcessResult = SetupTokenUnreadable()
        else:
            completed = self._capture_setup_output(
                [resolved, "setup-token"],
                timeout,
            )
        if not isinstance(completed, _CapturedSetupOutput):
            return completed
        if completed.return_code != 0:
            return SetupTokenRejected(completed.return_code)
        try:
            output = completed.output.decode("utf-8")
        except UnicodeDecodeError:
            return SetupTokenUnreadable()
        if (match := self.token_pattern.search(output)) is None:
            return SetupTokenMissing()
        try:
            token = validate_setup_token(match.group(0))
        except ProviderBoundaryError:
            return SetupTokenUnreadable()
        return SetupTokenSuccess(token)

    @staticmethod
    def _capture_setup_output(
        command: list[str],
        timeout: int,
    ) -> _SetupProcessResult:
        """Capture at most the bounded token-search input."""
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError, subprocess.SubprocessError:
            return SetupTokenUnreadable()
        output = bytearray()
        overflow = Event()
        read_failed = Event()
        stdout = process.stdout
        if stdout is None:
            _terminate_process(process)
            return SetupTokenUnreadable()

        reader = _start_output_reader(
            stdout,
            process,
            output,
            overflow,
            read_failed,
        )
        if reader is None:
            _terminate_process(process)
            stdout.close()
            return SetupTokenUnreadable()
        failure: SetupTokenTimedOut | SetupTokenUnreadable | None = None
        return_code: int | None = None
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process(process)
            failure = SetupTokenTimedOut()
        except OSError, subprocess.SubprocessError:
            _terminate_process(process)
            failure = SetupTokenUnreadable()
        reader.join()
        stdout.close()
        if failure is not None:
            return failure
        if return_code is None or overflow.is_set() or read_failed.is_set():
            return SetupTokenUnreadable()
        return _CapturedSetupOutput(return_code, bytes(output))


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    with suppress(OSError):
        process.kill()


def _drain_bounded_output(
    stdout: IO[bytes],
    process: subprocess.Popen[bytes],
    output: bytearray,
    overflow: Event,
    read_failed: Event,
) -> None:
    try:
        while chunk := stdout.read(_SETUP_READ_CHUNK_BYTES):
            remaining = _MAX_SETUP_OUTPUT_BYTES - len(output)
            output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                overflow.set()
                _kill_process(process)
                return
    except OSError:
        read_failed.set()
        _kill_process(process)


def _start_output_reader(
    stdout: IO[bytes],
    process: subprocess.Popen[bytes],
    output: bytearray,
    overflow: Event,
    read_failed: Event,
) -> Thread | None:
    reader = Thread(
        target=_drain_bounded_output,
        args=(stdout, process, output, overflow, read_failed),
        daemon=True,
    )
    try:
        reader.start()
    except RuntimeError:
        return None
    return reader


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    _kill_process(process)
    with suppress(OSError, subprocess.SubprocessError):
        process.wait()


__all__ = [
    "ClaudeProvider",
    "ClaudeSetupToken",
    "SetupTokenCapture",
    "SetupTokenMissing",
    "SetupTokenRejected",
    "SetupTokenSuccess",
    "SetupTokenTimedOut",
    "SetupTokenUnreadable",
]

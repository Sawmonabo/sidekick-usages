"""Claude provider facade and typed refresh workflow."""

import os
import platform
import shutil
import subprocess
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import Event, Thread
from typing import IO, Protocol

from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import (
    ExpiredExpiry,
    KnownExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    DetectedCredentials,
    UsageReport,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import AuthError, TransientError
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.providers.base import (
    CredentialDetection,
    CredentialStageReader,
    Provider,
    ProviderAuthenticatedAccount,
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureCause,
    ProviderFailureKind,
    RefreshResult,
    RefreshSuccess,
    runtime_account,
)
from sidekick_usages.providers.claude.credential_schemas import (
    CLAUDE_TOKEN_PATTERN,
    parse_refresh_credentials,
    validate_setup_token,
)
from sidekick_usages.providers.claude.credentials import (
    detect_credentials,
    parse_detected_credentials,
    require_claude_credentials,
)
from sidekick_usages.providers.claude.schemas import claude_failure
from sidekick_usages.providers.claude.usage import (
    fetch_usage as fetch_claude_usage,
)

OAUTH_REFRESH_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_REFRESH_EXPIRES_IN_SECONDS = 31_536_000
_MAX_SETUP_OUTPUT_BYTES = 1024 * 1024
_SETUP_READ_CHUNK_BYTES = 8192
_PRIVATE_CHILD_UMASK = 0o077
_INHERITED_REFRESH_ENVIRONMENT = (
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
)


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


def _closed_refresh_environment(
    isolated_home: Path,
    refresh_token: str,
    scopes: tuple[str, ...],
) -> dict[str, str]:
    """Build the closed child environment required on supported systems.

    Only process lookup, Windows process startup, locale, and temporary-file
    variables are inherited. Claude and Anthropic configuration or credential
    inputs can therefore enter only through the managed values below.
    """
    environment = {
        name: value
        for name in _INHERITED_REFRESH_ENVIRONMENT
        if (value := os.environ.get(name)) is not None
    }
    environment.update(
        {
            "HOME": str(isolated_home),
            "USERPROFILE": str(isolated_home),
            "APPDATA": str(isolated_home / "AppData" / "Roaming"),
            "LOCALAPPDATA": str(isolated_home / "AppData" / "Local"),
            "XDG_CONFIG_HOME": str(isolated_home / ".config"),
            "CLAUDE_CONFIG_DIR": str(isolated_home / ".claude"),
            "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": refresh_token,
            "CLAUDE_CODE_OAUTH_SCOPES": " ".join(scopes),
        }
    )
    return environment


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
            credentials=ClaudeSetupTokenCredentials(access_token=validated)
        )

    def _fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Fetch usage through the Claude credential-variant route."""
        return fetch_claude_usage(account, http)

    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        """Return a validated direct-HTTPS credential replacement."""
        credentials = require_claude_credentials(account)
        if isinstance(credentials, ClaudeSetupTokenCredentials):
            return claude_failure(
                ProviderFailureKind.MISSING,
                "Claude setup-token credentials have no refresh credential.",
                cause=(ProviderFailureCause.MISSING_REFRESH_CREDENTIAL),
            )
        if isinstance(
            classify_expiry(credentials.refresh_expiry, now=self.clock.now()),
            ExpiredExpiry,
        ):
            return claude_failure(
                ProviderFailureKind.EXPIRED,
                "The saved Claude login credential has expired.",
                cause=ProviderFailureCause.LOGIN_CREDENTIAL_EXPIRED,
            )
        return self._refresh_via_http(http, credentials)

    def refresh_credentials_in_stage(
        self,
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
        stage_home: Path,
        stage_reader: CredentialStageReader,
    ) -> RefreshResult:
        """Refresh using only one caller-owned private child home."""
        if not stage_home.is_absolute() or not stage_home.is_dir():
            return claude_failure(
                ProviderFailureKind.UNREADABLE,
                "Claude refresh staging is unavailable.",
                cause=ProviderFailureCause.REFRESH_PROCESS_UNAVAILABLE,
            )
        credentials = require_claude_credentials(runtime_account(account))
        if isinstance(credentials, ClaudeSetupTokenCredentials):
            return claude_failure(
                ProviderFailureKind.MISSING,
                "Claude setup-token credentials have no refresh credential.",
                cause=(ProviderFailureCause.MISSING_REFRESH_CREDENTIAL),
            )
        if isinstance(
            classify_expiry(credentials.refresh_expiry, now=self.clock.now()),
            ExpiredExpiry,
        ):
            return claude_failure(
                ProviderFailureKind.EXPIRED,
                "The saved Claude login credential has expired.",
                cause=ProviderFailureCause.LOGIN_CREDENTIAL_EXPIRED,
            )
        cli_result = self._refresh_via_cli(
            credentials,
            stage_home,
            stage_reader,
        )
        if cli_result is not None:
            return cli_result
        return self._refresh_via_http(http, credentials)

    def _refresh_via_cli(
        self,
        credentials: ClaudeLoginCredentials,
        isolated_home: Path,
        stage_reader: CredentialStageReader,
    ) -> RefreshResult | None:
        claude_bin = (
            None if platform.system() == "Darwin" else shutil.which("claude")
        )
        if claude_bin is None:
            return None
        scopes = self._refresh_scopes(credentials)
        env = _closed_refresh_environment(
            isolated_home,
            credentials.refresh_token,
            scopes,
        )
        completed = self._run_cli_refresh(
            claude_bin,
            env,
            isolated_home,
        )
        if isinstance(completed, ProviderFailure):
            return completed

        payload = stage_reader.read()
        if completed.return_code != 0 and payload is None:
            return claude_failure(
                ProviderFailureKind.REJECTED,
                "Claude rejected the saved subscription login.",
                cause=ProviderFailureCause.PROVIDER_REJECTED_REFRESH,
            )
        detected = (
            claude_failure(
                ProviderFailureKind.MISSING,
                "Claude credentials were not found.",
            )
            if payload is None
            else parse_detected_credentials(payload, self.clock.now())
        )
        if isinstance(detected, ProviderFailure):
            if detected.kind in {
                ProviderFailureKind.INCOMPLETE,
                ProviderFailureKind.MALFORMED,
            }:
                cause = (
                    ProviderFailureCause.REFRESH_OUTPUT_INCOMPLETE
                    if detected.kind is ProviderFailureKind.INCOMPLETE
                    else ProviderFailureCause.REFRESH_OUTPUT_MALFORMED
                )
                raise ProviderBoundaryError(
                    claude_failure(
                        detected.kind,
                        (
                            "Claude refresh output was incomplete."
                            if detected.kind is ProviderFailureKind.INCOMPLETE
                            else "Claude refresh output was malformed."
                        ),
                        cause=cause,
                        fields=detected.fields,
                    )
                ) from None
            if detected.kind is ProviderFailureKind.MISSING:
                return claude_failure(
                    ProviderFailureKind.INCOMPLETE,
                    "Claude CLI refresh produced no credentials.",
                    cause=ProviderFailureCause.REFRESH_OUTPUT_INCOMPLETE,
                )
            return detected
        return self._cli_refresh_success(credentials, detected)

    @staticmethod
    def _run_cli_refresh(
        claude_bin: str,
        env: dict[str, str],
        working_directory: Path,
    ) -> _CapturedSetupOutput | ProviderFailure:
        captured = ClaudeProvider._capture_setup_output(
            [claude_bin, "auth", "login", "--claudeai"],
            60,
            env=env,
            cwd=working_directory,
            umask=_PRIVATE_CHILD_UMASK if os.name == "posix" else -1,
        )
        if isinstance(captured, SetupTokenTimedOut):
            return claude_failure(
                ProviderFailureKind.UNREADABLE,
                "Claude credential refresh timed out.",
                cause=ProviderFailureCause.REFRESH_TIMED_OUT,
            )
        if isinstance(captured, SetupTokenUnreadable):
            return claude_failure(
                ProviderFailureKind.UNREADABLE,
                "Claude refresh process is unavailable.",
                cause=ProviderFailureCause.REFRESH_PROCESS_UNAVAILABLE,
            )
        return captured

    @staticmethod
    def _cli_refresh_success(
        previous: ClaudeLoginCredentials,
        detected: DetectedCredentials,
    ) -> RefreshSuccess:
        refreshed = detected.credentials
        if not isinstance(refreshed, ClaudeLoginCredentials):
            raise ProviderBoundaryError(
                claude_failure(
                    ProviderFailureKind.IDENTITY_MISMATCH,
                    "Claude refresh returned incompatible credentials.",
                    cause=(ProviderFailureCause.REFRESHED_IDENTITY_MISMATCH),
                )
            ) from None
        if (
            previous.identity is not None
            and refreshed.identity is not None
            and previous.identity != refreshed.identity
        ):
            raise ProviderBoundaryError(
                claude_failure(
                    ProviderFailureKind.IDENTITY_MISMATCH,
                    "Claude refresh returned a different login identity.",
                    cause=(ProviderFailureCause.REFRESHED_IDENTITY_MISMATCH),
                )
            ) from None
        credentials = replace(
            previous,
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token,
            access_expiry=refreshed.access_expiry,
            refresh_expiry=(
                refreshed.refresh_expiry
                if isinstance(refreshed.refresh_expiry, KnownExpiry)
                else previous.refresh_expiry
            ),
            scopes=refreshed.scopes,
            identity=refreshed.identity or previous.identity,
        )
        return RefreshSuccess(
            credentials=credentials,
            plan=None if detected.plan == "unknown" else detected.plan,
        )

    def _refresh_via_http(
        self,
        http: HttpClient,
        credentials: ClaudeLoginCredentials,
    ) -> RefreshResult:
        scopes = self._refresh_scopes(credentials)
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
                "Claude rejected the saved subscription login.",
                cause=ProviderFailureCause.PROVIDER_REJECTED_REFRESH,
            )
        except TransientError:
            return claude_failure(
                ProviderFailureKind.UNREADABLE,
                "Claude refresh is temporarily unavailable.",
                cause=(ProviderFailureCause.REFRESH_TEMPORARILY_UNAVAILABLE),
            )
        try:
            refreshed = parse_refresh_credentials(
                response,
                credentials,
                self.clock.now(),
            )
        except ProviderBoundaryError as error:
            cause = (
                ProviderFailureCause.REFRESH_OUTPUT_INCOMPLETE
                if error.failure.kind is ProviderFailureKind.INCOMPLETE
                else ProviderFailureCause.REFRESH_OUTPUT_MALFORMED
            )
            raise ProviderBoundaryError(
                claude_failure(
                    error.failure.kind,
                    (
                        "Claude refresh output was incomplete."
                        if error.failure.kind is ProviderFailureKind.INCOMPLETE
                        else "Claude refresh output was malformed."
                    ),
                    cause=cause,
                    fields=error.failure.fields,
                )
            ) from None
        return RefreshSuccess(credentials=refreshed)

    @staticmethod
    def _refresh_scopes(
        credentials: ClaudeLoginCredentials,
    ) -> tuple[str, ...]:
        return credentials.scopes

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
        *,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        umask: int = -1,
    ) -> _SetupProcessResult:
        """Capture at most the bounded token-search input."""
        try:
            process = subprocess.Popen(
                command,
                env=env,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                umask=umask,
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

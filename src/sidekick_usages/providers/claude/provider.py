"""Claude provider facade and typed refresh workflow."""

import os
from dataclasses import replace
from pathlib import Path

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
from sidekick_usages.http.client import HttpClient
from sidekick_usages.http.types import HttpOperation
from sidekick_usages.platform.host import detect_host_platform
from sidekick_usages.platform.types import HostPlatform
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
from sidekick_usages.providers.claude.auth.login.models import (
    ClaudeOfficialLoginResult,
)
from sidekick_usages.providers.claude.auth.login.service import (
    run_official_claude_login,
)
from sidekick_usages.providers.claude.credentials import (
    CLAUDE_SUBSCRIPTION_LOGIN_REJECTED,
    detect_credentials,
    native_claude_profile,
    parse_detected_credentials,
    require_claude_credentials,
    unreadable_credentials,
)
from sidekick_usages.providers.claude.environment import (
    claude_private_refresh_environment,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.executable import (
    discover_claude_executable,
)
from sidekick_usages.providers.claude.managed.types import ClaudeManagedFailure
from sidekick_usages.providers.claude.models import (
    SetupTokenMissing,
    SetupTokenRejected,
    SetupTokenSuccess,
    SetupTokenTimedOut,
    SetupTokenUnreadable,
)
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.schema.credentials import (
    CLAUDE_TOKEN_PATTERN,
    parse_refresh_credentials,
    validate_setup_token,
)
from sidekick_usages.providers.claude.schema.usage import claude_failure
from sidekick_usages.providers.claude.types import (
    ClaudeProcessFailure,
    SetupTokenCapture,
)
from sidekick_usages.providers.claude.usage import fetch_usage

OAUTH_REFRESH_ENDPOINT = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_REFRESH_EXPIRES_IN_SECONDS = 31_536_000
_MAX_SETUP_OUTPUT_BYTES = 1024 * 1024
_STAGED_KEYCHAIN_HOSTS = frozenset(
    {
        HostPlatform.MACOS_ARM64,
        HostPlatform.MACOS_X64,
    }
)


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
        try:
            profile = native_claude_profile(
                credential_home,
                environment=os.environ,
            )
        except ValueError:
            return unreadable_credentials()
        return detect_credentials(
            self.clock.now(),
            profile,
            environment=os.environ,
        )

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
        return fetch_usage(account, http)

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
        cli_result: RefreshResult | None = None
        if (
            detect_host_platform(environment=os.environ)
            not in _STAGED_KEYCHAIN_HOSTS
        ):
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
        scopes = self._refresh_scopes(credentials)
        environment = claude_private_refresh_environment(
            os.environ,
            process_home=isolated_home,
            config_directory=isolated_home / ".claude",
            refresh_token=credentials.refresh_token,
            scopes=scopes,
        )
        try:
            executable = discover_claude_executable(
                environment,
                working_directory=isolated_home,
                runner=run_bounded_claude_command,
            )
        except ClaudeManagedError:
            return None
        try:
            login_result = run_official_claude_login(
                executable,
                environment,
                isolated_home,
            )
        except ClaudeManagedError as error:
            if error.code is ClaudeManagedFailure.OFFICIAL_LOGIN_TIMED_OUT:
                message = "Claude credential refresh timed out."
                cause = ProviderFailureCause.REFRESH_TIMED_OUT
            else:
                message = "Claude refresh process is unavailable."
                cause = ProviderFailureCause.REFRESH_PROCESS_UNAVAILABLE
            return claude_failure(
                ProviderFailureKind.UNREADABLE,
                message,
                cause=cause,
            )

        if login_result is ClaudeOfficialLoginResult.FAILED:
            return claude_failure(
                ProviderFailureKind.UNREADABLE,
                "Claude refresh is temporarily unavailable.",
                cause=(ProviderFailureCause.REFRESH_TEMPORARILY_UNAVAILABLE),
                action_required=False,
            )
        payload = stage_reader.read()
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
                CLAUDE_SUBSCRIPTION_LOGIN_REJECTED,
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
        try:
            executable = discover_claude_executable(
                os.environ,
                runner=run_bounded_claude_command,
            )
            completed = run_bounded_claude_command(
                (str(executable.provenance.path), "setup-token"),
                timeout_seconds=timeout,
                maximum_output_bytes=_MAX_SETUP_OUTPUT_BYTES,
            )
        except ClaudeManagedError:
            return SetupTokenUnreadable()
        except ClaudeProcessError as error:
            return (
                SetupTokenTimedOut()
                if error.code is ClaudeProcessFailure.TIMED_OUT
                else SetupTokenUnreadable()
            )
        result: SetupTokenCapture
        if completed.return_code != 0:
            result = SetupTokenRejected(completed.return_code)
        else:
            result = self._setup_token_result(completed.output)
        return result

    def _setup_token_result(self, output_bytes: bytes) -> SetupTokenCapture:
        try:
            output = output_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return SetupTokenUnreadable()
        match = self.token_pattern.search(output)
        if match is None:
            return SetupTokenMissing()
        try:
            token = validate_setup_token(match.group(0))
        except ProviderBoundaryError:
            return SetupTokenUnreadable()
        return SetupTokenSuccess(token)

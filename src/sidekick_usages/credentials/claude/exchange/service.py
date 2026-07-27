"""Profile-neutral official Claude refresh-token exchange."""

from collections.abc import Mapping
from decimal import Decimal

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import (
    CredentialAction,
    CredentialHealth,
)
from sidekick_usages.credentials.claude.authority.types import (
    ClaudeAuthorityReader,
)
from sidekick_usages.credentials.claude.exchange.models import (
    ClaudeAuthorityExpectation,
    ClaudeExchangeFailure,
    ClaudeExchangeResult,
    ClaudeExchangeSuccess,
)
from sidekick_usages.credentials.claude.exchange.types import (
    ClaudeExchangeFailureKind,
    claude_exchange_storage_failure,
)
from sidekick_usages.providers.claude.auth.login.models import (
    ClaudeOfficialLoginResult,
)
from sidekick_usages.providers.claude.auth.login.service import (
    run_official_claude_login,
    verify_official_claude_login_status,
)
from sidekick_usages.providers.claude.auth.proof.service import (
    same_claude_authority_proof,
)
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
)
from sidekick_usages.providers.claude.environment import (
    claude_native_refresh_environment,
    claude_private_refresh_environment,
    claude_profile_environment,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedFailure,
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import (
    ClaudeManagedProfile,
    ClaudeNativeProfile,
)
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import (
    ClaudeCommandRunner,
    ClaudeProcessFailure,
)

_NATIVE_FILE_PLATFORMS = frozenset(
    {
        ClaudeManagedPlatform.LINUX_FILE,
        ClaudeManagedPlatform.WSL_FILE,
    }
)


class ClaudeOfficialLoginExchange:
    """Advance one Claude authority through the official login command."""

    def __init__(
        self,
        reader: ClaudeAuthorityReader,
        clock: Clock,
        *,
        environment: Mapping[str, str] | None = None,
        runner: ClaudeCommandRunner = run_bounded_claude_command,
    ) -> None:
        self._reader = reader
        self._clock = clock
        self._environment = environment
        self._runner = runner

    def provision(
        self,
        capabilities: ClaudeCapabilities,
        expectation: ClaudeAuthorityExpectation,
        refresh_token: str,
    ) -> ClaudeExchangeResult:
        """Exchange one leased refresh token and verify the final profile."""
        result = self._run_login(
            capabilities,
            expectation,
            refresh_token,
        )
        if isinstance(result, ClaudeExchangeFailure):
            return result
        if result is ClaudeOfficialLoginResult.FAILED:
            return self._failure_after_attempt(
                capabilities,
                expectation,
                ClaudeExchangeFailureKind.LOGIN_FAILED,
            )
        verification = self._verify_login(capabilities, expectation)
        if verification is not None:
            return verification
        return self._stable_post_login(capabilities, expectation)

    def _stable_post_login(
        self,
        capabilities: ClaudeCapabilities,
        expectation: ClaudeAuthorityExpectation,
    ) -> ClaudeExchangeResult:
        """Require two complete stable proofs after official login."""
        observed = self._read(capabilities, expectation)
        if isinstance(observed, ClaudeExchangeFailure):
            return observed
        confirmed = self._read(capabilities, expectation)
        if isinstance(confirmed, ClaudeExchangeFailure):
            return confirmed
        if (
            not same_claude_authority_proof(observed, confirmed)
            or observed.modified_milliseconds
            != confirmed.modified_milliseconds
        ):
            return ClaudeExchangeFailure(
                ClaudeExchangeFailureKind.RECONCILIATION_REQUIRED
            )
        if not claude_native_propagation_proven(
            capabilities,
            expectation.modified_milliseconds,
            confirmed.modified_milliseconds,
        ):
            return ClaudeExchangeFailure(
                ClaudeExchangeFailureKind.RECONCILIATION_REQUIRED
            )
        return verified_claude_exchange(expectation, confirmed)

    def _run_login(
        self,
        capabilities: ClaudeCapabilities,
        expectation: ClaudeAuthorityExpectation,
        refresh_token: str,
    ) -> ClaudeOfficialLoginResult | ClaudeExchangeFailure:
        """Run the official exchange in a credential-free environment."""
        environment: dict[str, str] = {}
        try:
            environment.update(
                self._refresh_environment(
                    capabilities,
                    expectation,
                    refresh_token,
                )
            )
            result = run_official_claude_login(
                capabilities.executable,
                environment,
                capabilities.profile.config_directory,
                runner=self._runner,
            )
        except ClaudeProcessError:
            return ClaudeExchangeFailure(
                ClaudeExchangeFailureKind.INCOMPATIBLE
            )
        except ClaudeManagedError as error:
            kind = (
                ClaudeExchangeFailureKind.TIMED_OUT
                if error.code is ClaudeManagedFailure.OFFICIAL_LOGIN_TIMED_OUT
                else ClaudeExchangeFailureKind.TRANSIENT
            )
            return self._failure_after_attempt(
                capabilities,
                expectation,
                kind,
            )
        finally:
            environment.clear()
        return result

    def _verify_login(
        self,
        capabilities: ClaudeCapabilities,
        expectation: ClaudeAuthorityExpectation,
    ) -> ClaudeExchangeFailure | None:
        """Verify that the official command left an authenticated profile."""
        environment: dict[str, str] = {}
        try:
            environment.update(self._profile_environment(capabilities))
            verify_official_claude_login_status(
                capabilities.executable,
                environment,
                capabilities.profile.config_directory,
                runner=self._runner,
            )
        except ClaudeManagedError:
            return self._failure_after_attempt(
                capabilities,
                expectation,
                ClaudeExchangeFailureKind.RECONCILIATION_REQUIRED,
            )
        except ClaudeProcessError:
            return ClaudeExchangeFailure(
                ClaudeExchangeFailureKind.INCOMPATIBLE
            )
        finally:
            environment.clear()
        return None

    def _refresh_environment(
        self,
        capabilities: ClaudeCapabilities,
        expectation: ClaudeAuthorityExpectation,
        refresh_token: str,
    ) -> dict[str, str]:
        profile = capabilities.profile
        if isinstance(profile, ClaudeManagedProfile):
            return claude_private_refresh_environment(
                self._environment,
                process_home=profile.config_directory,
                config_directory=profile.config_directory,
                refresh_token=refresh_token,
                scopes=expectation.scopes,
            )
        if isinstance(profile, ClaudeNativeProfile):
            return claude_native_refresh_environment(
                self._environment,
                process_home=profile.config_directory.parent,
                config_directory=profile.config_directory,
                refresh_token=refresh_token,
                scopes=expectation.scopes,
            )
        raise ClaudeProcessError(ClaudeProcessFailure.PROCESS_UNSAFE)

    def _profile_environment(
        self,
        capabilities: ClaudeCapabilities,
    ) -> dict[str, str]:
        return claude_profile_environment(
            self._environment,
            capabilities.profile,
        )

    def _failure_after_attempt(
        self,
        capabilities: ClaudeCapabilities,
        expectation: ClaudeAuthorityExpectation,
        failure: ClaudeExchangeFailureKind,
    ) -> ClaudeExchangeFailure:
        observed = self._read(capabilities, expectation)
        if isinstance(observed, ClaudeExchangeFailure):
            return observed
        if _matches_expectation(expectation, observed):
            return ClaudeExchangeFailure(failure)
        return ClaudeExchangeFailure(
            ClaudeExchangeFailureKind.RECONCILIATION_REQUIRED
        )

    def _read(
        self,
        capabilities: ClaudeCapabilities,
        expectation: ClaudeAuthorityExpectation,
    ) -> ClaudeAuthoritySnapshot | ClaudeExchangeFailure:
        try:
            return self._reader.read(
                capabilities,
                self._clock.now(),
                expected_identity=expectation.provider_identity,
                environment=self._environment,
                runner=self._runner,
            )
        except ClaudeProtectedStorageError as error:
            return ClaudeExchangeFailure(
                claude_exchange_storage_failure(error.code)
            )


def _invalid_exchange(
    expected: ClaudeAuthorityExpectation,
    observed: ClaudeAuthoritySnapshot,
) -> ClaudeExchangeFailureKind | None:
    if observed.generation == expected.generation:
        return ClaudeExchangeFailureKind.UNCHANGED
    if (
        observed.provider_identity != expected.provider_identity
        or observed.plan != expected.plan
        or frozenset(observed.scopes) != frozenset(expected.scopes)
        or observed.access_expires_at <= expected.access_expires_at
        or (
            expected.refresh_expires_at is not None
            and (
                observed.refresh_expires_at is None
                or observed.refresh_expires_at < expected.refresh_expires_at
            )
        )
        or observed.health is not CredentialHealth.HEALTHY
        or observed.action is not CredentialAction.NONE
    ):
        return ClaudeExchangeFailureKind.RECONCILIATION_REQUIRED
    return None


def verified_claude_exchange(
    expected: ClaudeAuthorityExpectation,
    observed: ClaudeAuthoritySnapshot,
) -> ClaudeExchangeResult:
    """Return one verified advanced generation or its exact failure."""
    invalid = _invalid_exchange(expected, observed)
    if invalid is not None:
        return ClaudeExchangeFailure(invalid)
    return ClaudeExchangeSuccess(observed)


def _matches_expectation(
    expected: ClaudeAuthorityExpectation,
    observed: ClaudeAuthoritySnapshot,
) -> bool:
    return (
        observed.provider_identity == expected.provider_identity
        and observed.generation == expected.generation
        and observed.plan == expected.plan
        and observed.access_expires_at == expected.access_expires_at
        and observed.refresh_expires_at == expected.refresh_expires_at
        and frozenset(observed.scopes) == frozenset(expected.scopes)
        and observed.modified_milliseconds == expected.modified_milliseconds
    )


def claude_native_propagation_proven(
    capabilities: ClaudeCapabilities,
    before: Decimal | None,
    after: Decimal | None,
) -> bool:
    """Require provider-visible ``mtimeMs`` advance on Linux and WSL."""
    if not isinstance(capabilities.profile, ClaudeNativeProfile):
        return True
    if capabilities.platform not in _NATIVE_FILE_PLATFORMS:
        return True
    return before is not None and after is not None and after > before


def claude_native_login_baseline_available(
    capabilities: ClaudeCapabilities,
    modified_milliseconds: Decimal | None,
) -> bool:
    """Return whether native login can prove platform propagation."""
    return (
        not isinstance(capabilities.profile, ClaudeNativeProfile)
        or capabilities.platform not in _NATIVE_FILE_PLATFORMS
        or modified_milliseconds is not None
    )

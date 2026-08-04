"""Authority-aware runtime credentials for managed Claude accounts."""

from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    AuthenticatedAccount,
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import AuthorityGeneration
from sidekick_usages.core.selection.models import FinalizedSelection
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.authorities import (
    AuthenticatedSavedAccount,
    CredentialAuthorityError,
    CredentialAuthorityFailureKind,
    CredentialLease,
)
from sidekick_usages.credentials.claude.authority.types import (
    ClaudeAuthorityMaintainer,
    ClaudeAuthorityReader,
)
from sidekick_usages.credentials.claude.managed.authority.service import (
    ClaudeManagedAuthorityReader,
    managed_authority_matches,
)
from sidekick_usages.credentials.claude.managed.maintenance.models import (
    ClaudeManagedAuthorityResult,
    require_managed_claude_authority,
)
from sidekick_usages.credentials.claude.managed.maintenance.types import (
    ClaudeManagedOutcome,
)
from sidekick_usages.credentials.claude.managed.profile import (
    ClaudeProfileCapabilityFactory,
)
from sidekick_usages.credentials.claude.native.authority.service import (
    ClaudeNativeAuthorityReader,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
)
from sidekick_usages.providers.claude.auth.storage.types import (
    ClaudeProtectedStorageFailure,
)
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.models import ClaudeNativeProfile
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner

_STORAGE_FAILURE_KINDS = {
    ClaudeProtectedStorageFailure.MISSING: (
        CredentialAuthorityFailureKind.MISSING
    ),
    ClaudeProtectedStorageFailure.MALFORMED: (
        CredentialAuthorityFailureKind.MALFORMED
    ),
    ClaudeProtectedStorageFailure.IDENTITY_MISMATCH: (
        CredentialAuthorityFailureKind.MISMATCH
    ),
    ClaudeProtectedStorageFailure.PROOF_CHANGED: (
        CredentialAuthorityFailureKind.MISMATCH
    ),
}
_MAINTENANCE_FAILURE_KINDS = {
    ClaudeManagedOutcome.FIXED_LIFETIME: (
        CredentialAuthorityFailureKind.MISMATCH
    ),
    ClaudeManagedOutcome.UNCHANGED: CredentialAuthorityFailureKind.MISMATCH,
    ClaudeManagedOutcome.LOGIN_REQUIRED: (
        CredentialAuthorityFailureKind.MISSING
    ),
    ClaudeManagedOutcome.INCOMPATIBLE: CredentialAuthorityFailureKind.MANAGED,
    ClaudeManagedOutcome.MALFORMED: CredentialAuthorityFailureKind.MALFORMED,
    ClaudeManagedOutcome.UNREADABLE: CredentialAuthorityFailureKind.UNREADABLE,
    ClaudeManagedOutcome.RECONCILIATION_REQUIRED: (
        CredentialAuthorityFailureKind.MISMATCH
    ),
    ClaudeManagedOutcome.TIMED_OUT: CredentialAuthorityFailureKind.UNREADABLE,
    ClaudeManagedOutcome.TRANSIENT: CredentialAuthorityFailureKind.UNREADABLE,
    ClaudeManagedOutcome.STATE_CHANGED: (
        CredentialAuthorityFailureKind.MISMATCH
    ),
}


class ClaudeManagedCredentialError(CredentialAuthorityError):
    """A managed Claude credential authority could not be opened safely."""

    def __init__(self, kind: CredentialAuthorityFailureKind) -> None:
        super().__init__(
            kind,
            "The managed Claude credential authority is unavailable.",
        )


class ClaudeManagedCredentialResolver:
    """Open selected native or inactive private Claude credentials."""

    def __init__(
        self,
        paths: ApplicationPaths,
        profiles: PrivateCredentialTree,
        selected: SelectedStateStore,
        maintainer: ClaudeAuthorityMaintainer,
        capabilities: ClaudeProfileCapabilityFactory,
        clock: Clock,
        *,
        environment: Mapping[str, str] | None = None,
        runner: ClaudeCommandRunner = run_bounded_claude_command,
    ) -> None:
        self._selected = selected
        self._maintainer = maintainer
        self._capabilities = capabilities
        self._clock = clock
        self._environment = environment
        self._runner = runner
        self._managed_reader = ClaudeManagedAuthorityReader(paths, profiles)

    def open_authorized(
        self,
        account: SavedAccount,
        authority: OperationAuthority,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        """Return one exact authority-bound Claude credential context."""
        return self._open(account, authority, None, maintain=True)

    def open_native_authorized(
        self,
        account: SavedAccount,
        expected_generation: AuthorityGeneration,
        authority: OperationAuthority,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        """Open the exact native target before durable finalization."""
        return self._open(
            account,
            authority,
            expected_generation,
            maintain=True,
        )

    def open_rollover_authorized(
        self,
        account: SavedAccount,
        expected_generation: AuthorityGeneration,
        authority: OperationAuthority,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        """Open one already-observed native refresh without maintenance."""
        return self._open(
            account,
            authority,
            expected_generation,
            maintain=False,
        )

    @contextmanager
    def _open(
        self,
        account: SavedAccount,
        authority: OperationAuthority,
        expected_native_generation: AuthorityGeneration | None,
        *,
        maintain: bool,
    ) -> Iterator[AuthenticatedSavedAccount]:
        authority.require(account.account_id)
        current = account
        if maintain:
            maintained = self._maintainer.maintain_with_authority(
                account.account_id,
                authority,
            )
            current = _maintained_account(account, maintained)
        subscription = require_managed_claude_authority(current)
        try:
            selected = None
            if expected_native_generation is None:
                selected = self._selected.load(ProviderId.CLAUDE)
                reader, capabilities = self._authority_source(
                    current,
                    selected,
                )
            else:
                reader, capabilities = self._native_authority_source()
            with reader.open_login(
                capabilities,
                self._clock.now(),
                expected_identity=subscription.provider_identity,
                environment=self._environment,
                runner=self._runner,
            ) as protected:
                if expected_native_generation is None:
                    self._require_projection(
                        current,
                        subscription,
                        selected,
                        protected.snapshot,
                    )
                elif (
                    protected.snapshot.provider_identity
                    != subscription.provider_identity
                    or protected.snapshot.generation
                    != expected_native_generation
                ):
                    raise ClaudeManagedCredentialError(
                        CredentialAuthorityFailureKind.MISMATCH
                    )
                lease = CredentialLease(
                    current,
                    current.account_id,
                    subscription.authority_id,
                    protected.credentials,
                )
                with lease:
                    yield AuthenticatedAccount(account=current, lease=lease)
        except ClaudeManagedError:
            raise ClaudeManagedCredentialError(
                CredentialAuthorityFailureKind.MANAGED
            ) from None
        except ClaudeProtectedStorageError as error:
            raise ClaudeManagedCredentialError(
                _STORAGE_FAILURE_KINDS.get(
                    error.code,
                    CredentialAuthorityFailureKind.UNREADABLE,
                )
            ) from None

    def _authority_source(
        self,
        account: SavedAccount,
        selected: FinalizedSelection | None,
    ) -> tuple[ClaudeAuthorityReader, ClaudeCapabilities]:
        if not _is_selected_account(account, selected):
            return (
                self._managed_reader,
                self._capabilities.managed(account.account_id),
            )
        return self._native_authority_source()

    def _native_authority_source(
        self,
    ) -> tuple[ClaudeAuthorityReader, ClaudeCapabilities]:
        native = self._capabilities.native(environment=self._environment)
        profile = native.profile
        if not isinstance(profile, ClaudeNativeProfile):
            raise ClaudeManagedCredentialError(
                CredentialAuthorityFailureKind.MANAGED
            )
        return ClaudeNativeAuthorityReader(profile), native

    @staticmethod
    def _require_projection(
        account: SavedAccount,
        subscription: ClaudeManagedLoginAuthority,
        selected: FinalizedSelection | None,
        snapshot: ClaudeAuthoritySnapshot,
    ) -> None:
        if _is_selected_account(account, selected):
            if (
                selected is None
                or subscription.provider_identity != snapshot.provider_identity
                or selected.generation != snapshot.generation
            ):
                raise ClaudeManagedCredentialError(
                    CredentialAuthorityFailureKind.MISMATCH
                )
            return
        if not managed_authority_matches(account, subscription, snapshot):
            raise ClaudeManagedCredentialError(
                CredentialAuthorityFailureKind.MISMATCH
            )


def _is_selected_account(
    account: SavedAccount,
    selected: FinalizedSelection | None,
) -> bool:
    """Return whether verified selected state names this saved account."""
    return selected is not None and selected.account_id == account.account_id


def _maintained_account(
    expected: SavedAccount,
    result: ClaudeManagedAuthorityResult,
) -> SavedAccount:
    """Return exact current metadata after successful official maintenance."""
    if result.outcome is not ClaudeManagedOutcome.HEALTHY:
        raise ClaudeManagedCredentialError(
            _MAINTENANCE_FAILURE_KINDS.get(
                result.outcome,
                CredentialAuthorityFailureKind.UNREADABLE,
            )
        )
    current = result.account
    try:
        expected_authority = require_managed_claude_authority(expected)
        current_authority = require_managed_claude_authority(current)
    except ValueError:
        raise ClaudeManagedCredentialError(
            CredentialAuthorityFailureKind.MISMATCH
        ) from None
    expected_account_authority = expected.authority
    current_account_authority = current.authority
    if (
        not isinstance(expected_account_authority, ClaudeAccountAuthority)
        or not isinstance(current_account_authority, ClaudeAccountAuthority)
        or current.account_id != expected.account_id
        or current.provider_id is not expected.provider_id
        or current.label != expected.label
        or current_account_authority.setup_token
        != expected_account_authority.setup_token
        or current_authority.authority_id != expected_authority.authority_id
        or current_authority.provider_identity
        != expected_authority.provider_identity
    ):
        raise ClaudeManagedCredentialError(
            CredentialAuthorityFailureKind.MISMATCH
        )
    return current

"""Operation-scoped credentials for managed Codex authorities."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager

from sidekick_usages.core.accounts.models import (
    AuthenticatedAccount,
    SavedAccount,
)
from sidekick_usages.core.models import CodexCredentials
from sidekick_usages.credentials.authorities import (
    AuthenticatedSavedAccount,
    CredentialAuthorityError,
    CredentialAuthorityFailureKind,
    CredentialLease,
)
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.credentials.codex.models import (
    CodexManagedAuthorityResult,
    require_managed_codex_authority,
)
from sidekick_usages.credentials.codex.types import CodexManagedOutcome
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
)

_FAILURE_KINDS = {
    CodexManagedOutcome.HEALTHY: CredentialAuthorityFailureKind.MISMATCH,
    CodexManagedOutcome.UNCHANGED: CredentialAuthorityFailureKind.MISMATCH,
    CodexManagedOutcome.REJECTED: CredentialAuthorityFailureKind.MISMATCH,
    CodexManagedOutcome.LOGGED_OUT: CredentialAuthorityFailureKind.MISSING,
    CodexManagedOutcome.INCOMPATIBLE: (CredentialAuthorityFailureKind.MANAGED),
    CodexManagedOutcome.MALFORMED: CredentialAuthorityFailureKind.MALFORMED,
    CodexManagedOutcome.TIMED_OUT: CredentialAuthorityFailureKind.UNREADABLE,
    CodexManagedOutcome.TRANSIENT: CredentialAuthorityFailureKind.UNREADABLE,
}


class CodexManagedCredentialError(CredentialAuthorityError):
    """A managed Codex projection could not be opened safely."""

    def __init__(self, outcome: CodexManagedOutcome) -> None:
        super().__init__(
            _FAILURE_KINDS[outcome],
            "The managed Codex credential authority is unavailable.",
        )


class CodexManagedCredentialResolver:
    """Open one managed Codex projection under an existing account lock."""

    def __init__(
        self,
        coordinator: CodexManagedAuthorityCoordinator,
        authority: OperationAuthority,
    ) -> None:
        self._coordinator = coordinator
        self._authority = authority

    def open(
        self,
        account: SavedAccount,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        """Return an unopened exact managed credential context."""
        return self._open(account)

    @contextmanager
    def _open(
        self,
        account: SavedAccount,
    ) -> Iterator[AuthenticatedSavedAccount]:
        managed = require_managed_codex_authority(account)
        self._authority.require(account.account_id)
        projection = self._coordinator.open_projection_with_authority(
            account.account_id,
            self._authority,
        )
        if isinstance(projection, CodexManagedAuthorityResult):
            raise CodexManagedCredentialError(projection.outcome)
        with projection:
            lease = CredentialLease(
                account,
                account.account_id,
                managed.authority_id,
                CodexCredentials(
                    access_token=projection.access_token,
                    expiry=projection.expiry,
                    account_id=str(projection.provider_identity),
                ),
            )
            with lease:
                yield AuthenticatedAccount(account=account, lease=lease)

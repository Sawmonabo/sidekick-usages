"""Operation-scoped access to protected account credentials."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from enum import StrEnum
from types import TracebackType
from typing import Protocol, Self

from sidekick_usages.core.accounts.models import (
    AuthenticatedAccount,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityId,
    SidekickAccountId,
)
from sidekick_usages.core.models import Account
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import UsageError
from sidekick_usages.persistence.accounts.runtime_bridge import (
    CredentialAuthorityUnavailableError,
    active_stored_reference,
    require_active_authority_kind,
    runtime_account_from_saved,
)
from sidekick_usages.persistence.credentials.repository import (
    CredentialAuthorityRepository,
)
from sidekick_usages.persistence.errors import (
    InvalidSchemaError,
    PersistenceFilesystemError,
)
from sidekick_usages.persistence.models.credential import (
    StoredCredentialAuthority,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)

type AuthenticatedSavedAccount = AuthenticatedAccount[CredentialLease]
type CredentialLeaseFactory = Callable[
    [SavedAccount],
    AbstractContextManager[AuthenticatedSavedAccount],
]


class CredentialAuthorityFailureKind(StrEnum):
    """Closed failures for resolving one credential authority."""

    MISSING = "missing"
    MALFORMED = "malformed"
    UNREADABLE = "unreadable"
    RETIRED = "retired"
    MANAGED = "managed"
    MISMATCH = "mismatch"
    CLOSED = "closed"


class CredentialAuthorityError(UsageError):
    """Secret-safe base failure for credential authority access."""

    kind: CredentialAuthorityFailureKind

    def __init__(
        self,
        kind: CredentialAuthorityFailureKind,
        message: str,
    ) -> None:
        self.kind = kind
        super().__init__(message)


class MissingCredentialAuthorityError(CredentialAuthorityError):
    """The referenced credential authority does not exist."""

    def __init__(self) -> None:
        super().__init__(
            CredentialAuthorityFailureKind.MISSING,
            "The saved account credential authority is missing.",
        )


class MalformedCredentialAuthorityError(CredentialAuthorityError):
    """The referenced credential authority failed strict validation."""

    def __init__(self) -> None:
        super().__init__(
            CredentialAuthorityFailureKind.MALFORMED,
            "The saved account credential authority is malformed.",
        )


class UnreadableCredentialAuthorityError(CredentialAuthorityError):
    """The referenced credential authority cannot be read safely."""

    def __init__(self) -> None:
        super().__init__(
            CredentialAuthorityFailureKind.UNREADABLE,
            "The saved account credential authority is unreadable.",
        )


class RetiredCredentialAuthorityError(CredentialAuthorityError):
    """The referenced credential authority was intentionally retired."""

    def __init__(self) -> None:
        super().__init__(
            CredentialAuthorityFailureKind.RETIRED,
            "The saved account credential authority is retired.",
        )


class ManagedCredentialAuthorityError(CredentialAuthorityError):
    """A provider-managed authority requires its provider resolver."""

    def __init__(self) -> None:
        super().__init__(
            CredentialAuthorityFailureKind.MANAGED,
            "The saved account uses a provider-managed credential authority.",
        )


class MismatchedCredentialAuthorityError(CredentialAuthorityError):
    """Authority binding does not match the requested saved account."""

    def __init__(self) -> None:
        super().__init__(
            CredentialAuthorityFailureKind.MISMATCH,
            "The saved account credential authority binding does not match.",
        )


class ClosedCredentialLeaseError(CredentialAuthorityError):
    """Credential material was requested outside its operation scope."""

    def __init__(self) -> None:
        super().__init__(
            CredentialAuthorityFailureKind.CLOSED,
            "The credential lease is not active.",
        )


class CredentialAuthorityReader(Protocol):
    """Read one qualified authority without exposing persistence details."""

    def read(self, account: SavedAccount) -> StoredCredentialAuthority:
        """Return the exact protected authority for ``account``."""


class CredentialResolver(Protocol):
    """Open operation-scoped credentials for a secret-free account."""

    def open(
        self,
        account: SavedAccount,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        """Return one unopened authenticated-account context."""


class SavedAccountSource(Protocol):
    """Expose the stable no-secret current account index."""

    def saved_accounts(self) -> tuple[SavedAccount, ...]:
        """Return the loaded stable account index."""


class ProtectedCredentialAuthorityReader:
    """Read stored authorities through qualified protected persistence."""

    def __init__(self, repository: CredentialAuthorityRepository) -> None:
        self._repository = repository

    def read(self, account: SavedAccount) -> StoredCredentialAuthority:
        """Read and validate the account's exact active authority."""
        try:
            authority_id = active_stored_reference(account)
            authority = self._repository.read(
                account.account_id,
                authority_id,
            )
            if authority is None:
                raise MissingCredentialAuthorityError
            require_active_authority_kind(account, authority)
        except CredentialAuthorityUnavailableError:
            raise ManagedCredentialAuthorityError from None
        except InvalidSchemaError:
            raise MalformedCredentialAuthorityError from None
        except PersistenceFilesystemError:
            raise UnreadableCredentialAuthorityError from None
        if (
            authority.account_id != account.account_id
            or authority.provider_id is not account.provider_id
        ):
            raise MismatchedCredentialAuthorityError
        return authority


class CredentialLease:
    """Context-managed runtime account whose representation is redacted."""

    __slots__ = (
        "_account_id",
        "_authority",
        "_authority_id",
        "_closed",
        "_provider_id",
        "_runtime",
        "_saved",
    )

    def __init__(
        self,
        account: SavedAccount,
        authority: StoredCredentialAuthority,
    ) -> None:
        """Bind one protected authority to its exact saved account."""
        try:
            expected_authority_id = active_stored_reference(account)
        except CredentialAuthorityUnavailableError:
            raise ManagedCredentialAuthorityError from None
        except InvalidSchemaError:
            raise MalformedCredentialAuthorityError from None
        if (
            authority.account_id != account.account_id
            or authority.provider_id is not account.provider_id
            or authority.authority_id != expected_authority_id
        ):
            raise MismatchedCredentialAuthorityError
        self._account_id = account.account_id
        self._authority_id = authority.authority_id
        self._provider_id = account.provider_id
        self._authority: StoredCredentialAuthority | None = authority
        self._saved: SavedAccount | None = account
        self._runtime: Account | None = None
        self._closed = False

    @property
    def account_id(self) -> SidekickAccountId:
        """Return the non-secret bound account identifier."""
        return self._account_id

    @property
    def authority_id(self) -> AuthorityId:
        """Return the non-secret bound authority identifier."""
        return self._authority_id

    @property
    def provider_id(self) -> ProviderId:
        """Return the non-secret bound provider."""
        return self._provider_id

    @property
    def account(self) -> Account:
        """Return credentials only while the lease context is active."""
        if self._runtime is None:
            raise ClosedCredentialLeaseError
        return self._runtime

    def __enter__(self) -> Self:
        """Open this lease exactly once."""
        if self._closed or self._runtime is not None:
            raise ClosedCredentialLeaseError
        authority = self._authority
        if authority is None:
            raise ClosedCredentialLeaseError
        try:
            runtime = runtime_account_from_saved(
                self._saved_account(),
                authority.credentials,
            )
        except BaseException:
            self._authority = None
            self._saved = None
            self._closed = True
            raise
        self._runtime = runtime
        self._authority = None
        self._saved = None
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the lease and release its credential references."""
        del exc_type, exc_value, traceback
        self._runtime = None
        self._authority = None
        self._saved = None
        self._closed = True

    def _saved_account(self) -> SavedAccount:
        """Return metadata retained only until the lease opens."""
        saved = self._saved
        if saved is None:
            raise ClosedCredentialLeaseError
        return saved

    def __repr__(self) -> str:
        """Return a representation that never includes credential material."""
        return "<CredentialLease redacted>"


class AuthenticatedAccountResolver:
    """Open one credential lease for one saved-account operation."""

    def __init__(self, reader: CredentialAuthorityReader) -> None:
        self._reader = reader

    def open(
        self,
        account: SavedAccount,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        """Return an unopened context for one exact saved account."""
        return self._open(account)

    @contextmanager
    def _open(
        self,
        account: SavedAccount,
    ) -> Iterator[AuthenticatedSavedAccount]:
        authority = self._reader.read(account)
        lease = CredentialLease(account, authority)
        with lease:
            yield AuthenticatedAccount(account=account, lease=lease)


def credential_resolver_for(
    source: SavedAccountSource,
    tree: PrivateCredentialTree,
) -> CredentialResolver:
    """Compose protected leases for the current stable account source."""
    source.saved_accounts()
    return AuthenticatedAccountResolver(
        ProtectedCredentialAuthorityReader(CredentialAuthorityRepository(tree))
    )

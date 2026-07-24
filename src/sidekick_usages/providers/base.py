"""Provider abstraction.

Provider integrations implement :class:`Provider`, allowing application
services and commands to dispatch through a shared capability contract.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.models import (
    Account,
    Credentials,
    DetectedCredentials,
    UsageReport,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import UsageError
from sidekick_usages.http.client import HttpClient

type CredentialDetection = DetectedCredentials | ProviderFailure
type RefreshResult = RefreshSuccess | ProviderFailure


class ProviderFailureKind(StrEnum):
    """Closed safe failure states owned by provider integrations."""

    MISSING = "missing"
    UNREADABLE = "unreadable"
    MALFORMED = "malformed"
    INCOMPLETE = "incomplete"
    EXPIRED = "expired"
    REJECTED = "rejected"
    IDENTITY_MISMATCH = "identity_mismatch"
    UNSUPPORTED = "unsupported"


class ProviderFailureCause(StrEnum):
    """Closed refresh causes without presentation-owned recovery copy."""

    MISSING_REFRESH_CREDENTIAL = "missing refresh credential"
    ACCESS_CREDENTIAL_EXPIRED = "access credential expired"
    LOGIN_CREDENTIAL_EXPIRED = "login credential expired"
    PROVIDER_REJECTED_REFRESH = "provider rejected refresh"
    REFRESH_TIMED_OUT = "refresh timed out"
    REFRESH_PROCESS_UNAVAILABLE = "refresh process unavailable"
    REFRESH_OUTPUT_INCOMPLETE = "refresh output incomplete"
    REFRESH_OUTPUT_MALFORMED = "refresh output malformed"
    REFRESHED_IDENTITY_MISMATCH = "refreshed identity mismatch"
    REFRESH_TEMPORARILY_UNAVAILABLE = "refresh temporarily unavailable"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderFailure:
    """Secret-safe provider failure suitable for application boundaries."""

    provider_id: ProviderId
    kind: ProviderFailureKind
    message: str
    cause: ProviderFailureCause | None = None
    action_required: bool = True
    fields: tuple[str, ...] = ()


class ProviderBoundaryError(UsageError):
    """An untrusted provider payload violated its owning schema."""

    def __init__(self, failure: ProviderFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class CredentialStageReader(Protocol):
    """Read child-produced credentials through their qualified owner."""

    def read(self) -> bytes | None:
        """Return bounded qualified bytes or absence."""


class CredentialAccountLease(Protocol):
    """Expose one runtime account only while its credential lease is active."""

    @property
    def account(self) -> Account:
        """Return the operation-scoped credential-bearing account."""


class ProviderAuthenticatedAccount(Protocol):
    """Worker-only saved account paired with one active credential lease."""

    @property
    def account(self) -> SavedAccount:
        """Return the secret-free saved-account record."""

    @property
    def lease(self) -> CredentialAccountLease:
        """Return the active operation-scoped credential lease."""


def runtime_account(account: ProviderAuthenticatedAccount) -> Account:
    """Return the runtime account through its active credential lease."""
    return account.lease.account


@dataclass(frozen=True, slots=True, kw_only=True)
class RefreshSuccess:
    """Validated replacement credentials from one provider refresh."""

    credentials: Credentials = field(repr=False)
    plan: str | None = None


class Provider(ABC):
    """Abstract base class for one AI assistant integration.

    Subclasses must define :attr:`id`, :attr:`display_name`, and
    :attr:`token_pattern`, and implement the four abstract methods.
    """

    #: Stable provider id, used as a dict/config key.
    id: ProviderId

    #: Human-readable provider name for error messages and help.
    display_name: str = ""

    #: Compiled regex that recognizes a valid token shape.
    token_pattern: re.Pattern[str] = re.compile(r"")

    @abstractmethod
    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        """Read OAuth credentials from the local provider install.

        :param credential_home: Optional provider state directory to
            inspect instead of the default install location. Providers
            that do not support multiple state homes may ignore it.
        :return: Detected credentials or one explicit safe failure.
        """

    @abstractmethod
    def credentials_from_token(self, token: str) -> CredentialDetection:
        """Validate one manually supplied token at its owning boundary."""

    def fetch_usage(
        self,
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
    ) -> UsageReport:
        """Call the provider usage endpoint within an active lease.

        :param account: Authenticated saved account to query.
        :param http: Shared HTTP client (handles retries).
        :return: Parsed usage report.
        :raises AuthError: If the token is rejected.
        :raises RateLimitError: If rate-limited after retries.
        :raises TransientError: On 5xx or network failure.
        """
        return self._fetch_usage(runtime_account(account), http)

    @abstractmethod
    def _fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Implement provider usage against one active runtime account."""

    def validate_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Validate an unsaved credential candidate without a saved lease."""
        return self._fetch_usage(account, http)

    def refresh_credentials(
        self,
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
    ) -> RefreshResult:
        """Return refreshed credentials without mutating ``account``.

        :param account: Authenticated saved account whose token to refresh.
        :param http: Shared HTTP client.
        :return: Validated replacement credentials or a safe failure.
        """
        return self._refresh_credentials(runtime_account(account), http)

    @abstractmethod
    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        """Implement refresh against one active runtime account."""

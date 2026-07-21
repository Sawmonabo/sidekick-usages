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

from sidekick_usages.core.models import (
    Account,
    Credentials,
    DetectedCredentials,
    UsageReport,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import UsageError
from sidekick_usages.http import HttpClient


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


@dataclass(frozen=True, slots=True, kw_only=True)
class RefreshSuccess:
    """Validated replacement credentials from one provider refresh."""

    credentials: Credentials = field(repr=False)
    plan: str | None = None


type CredentialDetection = DetectedCredentials | ProviderFailure
type RefreshResult = RefreshSuccess | ProviderFailure


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

    @abstractmethod
    def fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Call the provider's usage endpoint for one account.

        :param account: Account to query.
        :param http: Shared HTTP client (handles retries).
        :return: Parsed usage report.
        :raises AuthError: If the token is rejected.
        :raises RateLimitError: If rate-limited after retries.
        :raises TransientError: On 5xx or network failure.
        """

    @abstractmethod
    def refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        """Return refreshed credentials without mutating ``account``.

        :param account: Account whose token to refresh. Mutated
            only by the application after a successful result.
        :param http: Shared HTTP client.
        :return: Validated replacement credentials or a safe failure.
        """

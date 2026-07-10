"""Provider abstraction.

Provider integrations implement :class:`Provider`, allowing application
services and commands to dispatch through a shared capability contract.
"""

import re
from abc import ABC, abstractmethod
from pathlib import Path

from sidekick_usages.core.models import (
    Account,
    DetectedCredentials,
    UsageReport,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.http import HttpClient


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
    ) -> DetectedCredentials | None:
        """Read OAuth credentials from the local provider install.

        :param credential_home: Optional provider state directory to
            inspect instead of the default install location. Providers
            that do not support multiple state homes may ignore it.
        :return: Detected credentials, or ``None`` when no local
            login is found.
        """

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
    def refresh_token(
        self,
        account: Account,
        http: HttpClient,
    ) -> bool:
        """Refresh the access token using the stored refresh token.

        Providers or account types without refresh support should
        return ``False`` immediately and let the caller raise an auth
        error.

        :param account: Account whose token to refresh. Mutated
            in-place on success.
        :param http: Shared HTTP client.
        :return: True on successful refresh, False otherwise.
        """

    @abstractmethod
    def run_setup_token(self) -> str | None:
        """Run the provider's long-lived token generator.

        :return: A token string, or ``None`` on failure.
        :raises UnsupportedOperationError: When the provider has no
            equivalent of ``claude setup-token`` (Codex).
        """

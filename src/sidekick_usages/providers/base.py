"""Provider abstraction.

Each provider (Claude Code, Codex CLI, ...) implements the
:class:`Provider` ABC. The CLI dispatches calls through this
interface so adding a new provider means adding one file, not
refactoring the rest of the codebase.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.http import HttpClient
from sidekick_usages.report import UsageReport
from sidekick_usages.store import Account


@dataclass
class DetectedCredentials:
    """Credentials extracted from a provider's local install.

    :ivar access_token: OAuth access token (bearer auth).
    :ivar provider_account_id: Provider-native account/workspace id
        needed by APIs that require an account header in addition to
        bearer auth. Codex uses this for ``ChatGPT-Account-Id``.
    :ivar refresh_token: Refresh token, or ``None`` if absent.
    :ivar expires_at: Unix timestamp of access-token expiry, or
        ``None`` when unknown.
    :ivar plan: Plan tag (``"max"``, ``"plus"``, etc.). May be
        ``"unknown"`` when the local creds don't expose it.
    :ivar scopes: OAuth scope list when surfaced by the local
        credentials file (Claude's ``~/.claude/.credentials.json``
        exposes ``scopes``; not all providers do). ``None`` when
        unknown.
    :ivar id_token: Provider id token when the local credential store
        exposes it. Codex needs this to reconstruct a CLI-compatible
        file-backed ``auth.json``.
    :ivar last_refresh: Provider-native last-refresh timestamp, when
        present in the local credential store.
    """

    access_token: str
    provider_account_id: str | None = None
    refresh_token: str | None = None
    expires_at: int | None = None
    plan: str = "unknown"
    scopes: list[str] | None = None
    id_token: str | None = None
    last_refresh: str | None = None


class Provider(ABC):
    """Abstract base class for one AI assistant integration.

    Subclasses must define :attr:`id`, :attr:`display_name`, and
    :attr:`token_pattern`, and implement the four abstract methods.
    """

    #: Stable provider id, used as a dict/config key.
    id: str = ""

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

"""Provider-neutral runtime product models."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from typing import ClassVar, assert_never

from sidekick_usages.core.accounts.types import (
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.expiry import Expiry, KnownExpiry, UnknownExpiry
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import (
    AccountLabel,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
    TokenActivityScope,
)

_MAX_CLAUDE_IDENTITY_BYTES = 4_096
_MAX_CLAUDE_SCOPES = 128
_CLAUDE_PROFILE_SCOPE = "user:profile"


type ClaudeCredentials = ClaudeSetupTokenCredentials | ClaudeLoginCredentials
type Credentials = ClaudeCredentials | CodexCredentials
type TokenActivityReading = TokenActivitySummary | TokenActivityUnavailable


def _require_bounded_claude_identity(value: str) -> None:
    """Reject identity values that are empty, malformed, or unbounded."""
    if not isinstance(value, str):
        raise TypeError("Claude identity values must be strings.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(
            "Claude identity values must be valid UTF-8."
        ) from None
    if not encoded or len(encoded) > _MAX_CLAUDE_IDENTITY_BYTES:
        raise ValueError(
            "Claude identity values must be nonempty and bounded."
        )


def _require_claude_token(value: str, *, kind: str) -> None:
    """Require one nonempty string credential at the domain boundary."""
    if not isinstance(value, str):
        raise TypeError(f"Claude {kind} tokens must be strings.")
    if not value:
        raise ValueError(f"Claude {kind} tokens must be nonempty.")


def _require_claude_login_scopes(scopes: tuple[str, ...]) -> None:
    """Require the unique capabilities needed by a Claude login."""
    if not isinstance(scopes, tuple):
        raise TypeError("Claude login scopes must be a tuple.")
    if (
        not scopes
        or len(scopes) > _MAX_CLAUDE_SCOPES
        or len(scopes) != len(set(scopes))
        or _CLAUDE_PROFILE_SCOPE not in scopes
    ):
        raise ValueError(
            "Claude login scopes must be nonempty, unique, and include "
            "user:profile."
        )
    for scope in scopes:
        _require_bounded_claude_identity(scope)


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeSetupTokenCredentials:
    """Claude setup-token material without saved-login state."""

    provider_id: ClassVar[ProviderId] = ProviderId.CLAUDE

    access_token: str = field(repr=False)

    def __post_init__(self) -> None:
        """Require explicit nonempty setup-token material."""
        _require_claude_token(self.access_token, kind="access")


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeLoginIdentity:
    """Stable provider-owned identity for one Claude login."""

    account_id: str = field(repr=False)
    organization_id: str = field(repr=False)

    def __post_init__(self) -> None:
        """Require complete bounded stable identity values."""
        _require_bounded_claude_identity(self.account_id)
        _require_bounded_claude_identity(self.organization_id)

    @property
    def provider_identity(self) -> ProviderIdentity:
        """Return the canonical collision-safe provider identity."""
        account_bytes = self.account_id.encode("utf-8")
        return ProviderIdentity(
            f"{len(account_bytes)}:{self.account_id}{self.organization_id}"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeLoginCredentials:
    """Complete refreshable Claude subscription-login credentials."""

    provider_id: ClassVar[ProviderId] = ProviderId.CLAUDE

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_expiry: KnownExpiry
    refresh_expiry: KnownExpiry | UnknownExpiry
    scopes: tuple[str, ...]
    identity: ClaudeLoginIdentity | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Reject incomplete login material and ambiguous capabilities."""
        _require_claude_token(self.access_token, kind="access")
        _require_claude_token(self.refresh_token, kind="refresh")
        if not isinstance(self.access_expiry, KnownExpiry):
            raise TypeError("Claude login access expiry must be known.")
        if not isinstance(self.refresh_expiry, KnownExpiry | UnknownExpiry):
            raise TypeError("Claude login refresh expiry must be explicit.")
        _require_claude_login_scopes(self.scopes)
        if self.identity is not None and not isinstance(
            self.identity,
            ClaudeLoginIdentity,
        ):
            raise TypeError("Claude login identity must be complete.")


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexCredentials:
    """Codex credential material and provider-owned auth metadata."""

    provider_id: ClassVar[ProviderId] = ProviderId.CODEX

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expiry: Expiry = field(default_factory=UnknownExpiry)
    account_id: str | None = None
    auth_home: str | None = None
    id_token: str | None = field(default=None, repr=False)
    auth_last_refresh: str | None = None


def _refresh_token(credentials: Credentials) -> str | None:
    """Return refresh material only for credential kinds that own it."""
    match credentials:
        case ClaudeSetupTokenCredentials():
            return None
        case ClaudeLoginCredentials(refresh_token=refresh_token):
            return refresh_token
        case CodexCredentials(refresh_token=refresh_token):
            return refresh_token
        case unexpected:
            assert_never(unexpected)


def _access_expiry(credentials: Credentials) -> Expiry:
    """Return provider-neutral access-token expiry metadata."""
    match credentials:
        case ClaudeSetupTokenCredentials():
            return UnknownExpiry()
        case ClaudeLoginCredentials(access_expiry=access_expiry):
            return access_expiry
        case CodexCredentials(expiry=expiry):
            return expiry
        case unexpected:
            assert_never(unexpected)


def _claude_scopes(credentials: Credentials) -> tuple[str, ...] | None:
    """Return validated login scopes only for Claude login credentials."""
    match credentials:
        case ClaudeSetupTokenCredentials() | CodexCredentials():
            return None
        case ClaudeLoginCredentials(scopes=scopes):
            return scopes
        case unexpected:
            assert_never(unexpected)


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenActivitySummary:
    """One exact provider token total with its truthful scope."""

    total_tokens: int
    scope: TokenActivityScope
    since: date | None = None

    def __post_init__(self) -> None:
        """Reject values that cannot be a provider token total."""
        if (
            isinstance(self.total_tokens, bool)
            or not isinstance(self.total_tokens, int)
            or self.total_tokens < 0
        ):
            raise ValueError(
                "Token activity total must be a non-negative integer."
            )
        if not isinstance(self.scope, TokenActivityScope):
            raise TypeError("Token activity scope must be explicit.")


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountTokenActivitySnapshot:
    """One durable account-scoped provider activity observation."""

    provider_id: ProviderId
    provider_account_id: str = field(repr=False)
    summary: TokenActivitySummary
    fetched_at: datetime

    def __post_init__(self) -> None:
        """Require stable identity, account scope, and aware UTC time."""
        if not self.provider_account_id:
            raise ValueError("Activity snapshots require an account identity.")
        if self.summary.scope is not TokenActivityScope.ACCOUNT:
            raise ValueError("Activity snapshots must be account-scoped.")
        object.__setattr__(self, "fetched_at", as_utc(self.fetched_at))


@dataclass(frozen=True, slots=True, kw_only=True)
class TokenActivityUnavailable:
    """The provider has no authoritative activity reading."""

    scope: TokenActivityScope

    def __post_init__(self) -> None:
        """Require an explicit unavailable scope."""
        if not isinstance(self.scope, TokenActivityScope):
            raise TypeError("Token activity scope must be explicit.")


@dataclass(frozen=True, slots=True)
class DetectedCredentials:
    """Validated credentials extracted from a provider-owned source."""

    credentials: Credentials = field(repr=False)
    plan: str = "unknown"

    @property
    def provider_id(self) -> ProviderId:
        """Return the provider derived from the credential variant."""
        return self.credentials.provider_id

    @property
    def access_token(self) -> str:
        """Return the detected access token."""
        return self.credentials.access_token

    @property
    def refresh_token(self) -> str | None:
        """Return the detected refresh token when supplied."""
        return _refresh_token(self.credentials)

    @property
    def expiry(self) -> Expiry:
        """Return normalized detected expiry metadata."""
        return _access_expiry(self.credentials)

    @property
    def scopes(self) -> tuple[str, ...] | None:
        """Return Claude scopes, or ``None`` for Codex credentials."""
        return _claude_scopes(self.credentials)

    @property
    def provider_account_id(self) -> str | None:
        """Return the Codex account id when present."""
        if isinstance(self.credentials, CodexCredentials):
            return self.credentials.account_id
        return None

    @property
    def id_token(self) -> str | None:
        """Return the Codex id token when present."""
        if isinstance(self.credentials, CodexCredentials):
            return self.credentials.id_token
        return None

    @property
    def last_refresh(self) -> str | None:
        """Return opaque Codex auth refresh metadata when present."""
        if isinstance(self.credentials, CodexCredentials):
            return self.credentials.auth_last_refresh
        return None


@dataclass(slots=True, kw_only=True)
class Account:
    """One mutable saved account with provider-compatible credentials."""

    _AWARE_TIME_FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "last_heartbeat_at",
            "last_refresh_at",
        }
    )

    label: AccountLabel
    credentials: Credentials
    plan: str = "unknown"
    last_refresh_at: datetime | None = None
    last_refresh_status: RefreshStatus | None = None
    last_refresh_error: str | None = None
    heartbeat_enabled: bool = False
    heartbeat_window_resets: Mapping[str, datetime] | None = None
    heartbeat_targets: tuple[str, ...] | None = None
    last_heartbeat_at: datetime | None = None
    last_heartbeat_status: HeartbeatStatus | None = None
    last_heartbeat_error: str | None = None

    def __setattr__(self, name: str, value: object) -> None:
        """Preserve aware-UTC invariants across mutable runtime state."""
        if name in self._AWARE_TIME_FIELDS and value is not None:
            if not isinstance(value, datetime):
                raise TypeError(f"{name} must be a datetime or None.")
            value = as_utc(value)
        elif name == "heartbeat_window_resets" and value is not None:
            if not isinstance(value, Mapping):
                raise TypeError(
                    "heartbeat_window_resets must map strings to datetimes."
                )
            normalized: dict[str, datetime] = {}
            for target_id, reset_at in value.items():
                if not isinstance(target_id, str) or not isinstance(
                    reset_at, datetime
                ):
                    raise TypeError(
                        "heartbeat_window_resets must map strings to "
                        "datetimes."
                    )
                normalized[target_id] = as_utc(reset_at)
            value = MappingProxyType(normalized)
        object.__setattr__(self, name, value)

    @property
    def provider_id(self) -> ProviderId:
        """Return the provider derived from the credential variant."""
        return self.credentials.provider_id

    @property
    def access_token(self) -> str:
        """Return the active bearer token."""
        return self.credentials.access_token

    @property
    def refresh_token(self) -> str | None:
        """Return the saved refresh token when present."""
        return _refresh_token(self.credentials)

    @property
    def expiry(self) -> Expiry:
        """Return provider-neutral expiry metadata."""
        return _access_expiry(self.credentials)

    @property
    def scopes(self) -> tuple[str, ...] | None:
        """Return Claude scopes, or ``None`` for Codex credentials."""
        return _claude_scopes(self.credentials)

    @property
    def provider_account_id(self) -> str | None:
        """Return the Codex account id when present."""
        if isinstance(self.credentials, CodexCredentials):
            return self.credentials.account_id
        return None

    @property
    def codex_home(self) -> str | None:
        """Return the stored Codex auth-home locator when present."""
        if isinstance(self.credentials, CodexCredentials):
            return self.credentials.auth_home
        return None

    @property
    def codex_id_token(self) -> str | None:
        """Return the Codex id token when present."""
        if isinstance(self.credentials, CodexCredentials):
            return self.credentials.id_token
        return None

    @property
    def codex_last_refresh(self) -> str | None:
        """Return opaque Codex auth refresh metadata when present."""
        if isinstance(self.credentials, CodexCredentials):
            return self.credentials.auth_last_refresh
        return None


@dataclass(frozen=True, slots=True)
class UsageWindow:
    """One provider-normalized utilization window."""

    name: str
    utilization: float
    resets_at: datetime | None

    def __post_init__(self) -> None:
        if self.resets_at is not None:
            object.__setattr__(
                self,
                "resets_at",
                as_utc(self.resets_at),
            )

    @property
    def is_active(self) -> bool:
        """Return whether the window carries meaningful usage state."""
        return not (self.utilization == 0 and self.resets_at is None)


@dataclass(frozen=True, slots=True)
class UsageReport:
    """Provider-normalized usage data without raw provider payloads."""

    windows: tuple[UsageWindow, ...] = ()
    plan: str | None = None

    def active_windows(self) -> tuple[UsageWindow, ...]:
        """Return only windows carrying meaningful usage state."""
        return tuple(window for window in self.windows if window.is_active)


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountUsageSnapshot:
    """Last successful authenticated usage fetch for one stable account."""

    account_id: SidekickAccountId
    provider_id: ProviderId
    provider_identity: ProviderIdentity | None
    plan: str
    report: UsageReport
    fetched_at: datetime

    def __post_init__(self) -> None:
        """Normalize the exact observation time to aware UTC."""
        object.__setattr__(self, "fetched_at", as_utc(self.fetched_at))

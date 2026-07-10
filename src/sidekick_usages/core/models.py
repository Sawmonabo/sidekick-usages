"""Provider-neutral runtime product models."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import ClassVar

from sidekick_usages.core.expiry import Expiry, UnknownExpiry
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import (
    AccountLabel,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeCredentials:
    """Claude credential material and provider-owned metadata."""

    provider_id: ClassVar[ProviderId] = ProviderId.CLAUDE

    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expiry: Expiry = field(default_factory=UnknownExpiry)
    scopes: tuple[str, ...] | None = None


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


type Credentials = ClaudeCredentials | CodexCredentials


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
        return self.credentials.refresh_token

    @property
    def expiry(self) -> Expiry:
        """Return normalized detected expiry metadata."""
        return self.credentials.expiry

    @property
    def scopes(self) -> tuple[str, ...] | None:
        """Return Claude scopes, or ``None`` for Codex credentials."""
        if isinstance(self.credentials, ClaudeCredentials):
            return self.credentials.scopes
        return None

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
            "heartbeat_5h_reset_at",
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
    heartbeat_5h_reset_at: datetime | None = None
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
        return self.credentials.refresh_token

    @property
    def expiry(self) -> Expiry:
        """Return provider-neutral expiry metadata."""
        return self.credentials.expiry

    @property
    def scopes(self) -> tuple[str, ...] | None:
        """Return Claude scopes, or ``None`` for Codex credentials."""
        if isinstance(self.credentials, ClaudeCredentials):
            return self.credentials.scopes
        return None

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

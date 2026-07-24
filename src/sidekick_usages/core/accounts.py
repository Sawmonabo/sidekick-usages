"""Secret-free saved-account and credential-authority vocabulary."""

import unicodedata
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from typing import ClassVar, Self
from uuid import UUID

from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import (
    AccountLabel,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)

_MAX_OPAQUE_BYTES = 4_096
_MAX_METADATA_BYTES = 512


def _require_bounded_text(
    value: str,
    *,
    name: str,
    maximum: int,
) -> str:
    """Return one nonempty bounded UTF-8 value without control characters."""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{name} must be valid UTF-8.") from None
    if not encoded or len(encoded) > maximum:
        raise ValueError(f"{name} must be nonempty and bounded.")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError(f"{name} must not contain control characters.")
    return value


class _CanonicalUuid(str):
    """Canonical lower-case UUID identifier."""

    _name: ClassVar[str]

    def __new__(cls, value: str) -> Self:
        """Validate and construct one canonical UUID string."""
        if not isinstance(value, str):
            raise TypeError(f"{cls._name} must be a string.")
        try:
            parsed = UUID(value)
        except ValueError, AttributeError, TypeError:
            raise ValueError(
                f"{cls._name} must be a canonical UUID."
            ) from None
        if str(parsed) != value:
            raise ValueError(f"{cls._name} must be a canonical UUID.")
        return super().__new__(cls, value)


class SidekickAccountId(_CanonicalUuid):
    """Stable Sidekick-owned account identifier."""

    _name = "Sidekick account ID"


class AuthorityId(_CanonicalUuid):
    """Stable Sidekick-owned credential-authority identifier."""

    _name = "Authority ID"


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    """Bounded provider identity intentionally hidden from representations."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        """Require one complete bounded identity value."""
        _require_bounded_text(
            self.value,
            name="Provider identity",
            maximum=_MAX_OPAQUE_BYTES,
        )

    def __str__(self) -> str:
        """Return the opaque value only at a qualified boundary."""
        return self.value


@dataclass(frozen=True, slots=True)
class AuthorityGeneration:
    """Bounded provider generation hidden from representations."""

    value: str = field(repr=False)

    def __post_init__(self) -> None:
        """Require one complete bounded generation value."""
        _require_bounded_text(
            self.value,
            name="Authority generation",
            maximum=_MAX_OPAQUE_BYTES,
        )

    def __str__(self) -> str:
        """Return the opaque value only at a qualified boundary."""
        return self.value


class CredentialHealth(StrEnum):
    """Closed credential authority health states."""

    HEALTHY = "healthy"
    REFRESH_DUE = "refresh_due"
    LOGIN_REQUIRED = "login_required"
    MIGRATION_REQUIRED = "migration_required"
    UNREADABLE = "unreadable"
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    UNKNOWN = "unknown"


class MetricsFreshness(StrEnum):
    """Closed account metrics freshness states."""

    FRESH = "fresh"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


def _optional_utc(value: datetime | None) -> datetime | None:
    """Normalize an optional timestamp to aware UTC."""
    return None if value is None else as_utc(value)


def _safe_metadata(value: str | None, *, name: str) -> str | None:
    """Validate one optional non-secret metadata value."""
    if value is None:
        return None
    return _require_bounded_text(
        value,
        name=name,
        maximum=_MAX_METADATA_BYTES,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeSetupTokenAuthority:
    """Reference and fixed-lifetime metadata for one setup token."""

    authority_id: AuthorityId
    expires_at: datetime | None
    health: CredentialHealth
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        """Normalize fixed-lifetime observation timestamps."""
        object.__setattr__(self, "expires_at", _optional_utc(self.expires_at))
        object.__setattr__(
            self,
            "observed_at",
            _optional_utc(self.observed_at),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeLegacyLoginAuthority:
    """Reference-only metadata for a pre-managed Claude login."""

    authority_id: AuthorityId
    provider_identity: ProviderIdentity | None
    access_expires_at: datetime | None
    refresh_expires_at: datetime | None
    health: CredentialHealth
    observed_at: datetime | None = None
    kind: ClassVar[str] = "legacy"

    def __post_init__(self) -> None:
        """Normalize legacy login timestamps."""
        object.__setattr__(
            self,
            "access_expires_at",
            _optional_utc(self.access_expires_at),
        )
        object.__setattr__(
            self,
            "refresh_expires_at",
            _optional_utc(self.refresh_expires_at),
        )
        object.__setattr__(
            self,
            "observed_at",
            _optional_utc(self.observed_at),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeManagedLoginAuthority:
    """Provider-owned Claude profile metadata without credential values."""

    authority_id: AuthorityId
    provider_identity: ProviderIdentity
    generation: AuthorityGeneration
    verified_at: datetime
    executable_version: str
    health: CredentialHealth
    kind: ClassVar[str] = "managed"

    def __post_init__(self) -> None:
        """Normalize verification time and bound executable metadata."""
        object.__setattr__(self, "verified_at", as_utc(self.verified_at))
        _require_bounded_text(
            self.executable_version,
            name="Claude executable version",
            maximum=_MAX_METADATA_BYTES,
        )


type ClaudeSubscriptionAuthority = (
    ClaudeLegacyLoginAuthority | ClaudeManagedLoginAuthority
)


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudeAccountAuthority:
    """One logical Claude account's independent credential authorities."""

    setup_token: ClaudeSetupTokenAuthority | None = None
    subscription: ClaudeSubscriptionAuthority | None = None
    provider_id: ClassVar[ProviderId] = ProviderId.CLAUDE

    def __post_init__(self) -> None:
        """Reject a Claude account without any credential authority."""
        if self.setup_token is None and self.subscription is None:
            raise ValueError(
                "Claude accounts require a setup-token or subscription "
                "authority."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexLegacyAuthority:
    """Reference-only metadata for a pre-managed Codex login."""

    authority_id: AuthorityId
    provider_identity: ProviderIdentity | None
    expires_at: datetime | None
    generation: AuthorityGeneration | None
    health: CredentialHealth
    observed_at: datetime | None = None
    kind: ClassVar[str] = "legacy"

    def __post_init__(self) -> None:
        """Normalize legacy Codex timestamps."""
        object.__setattr__(self, "expires_at", _optional_utc(self.expires_at))
        object.__setattr__(
            self,
            "observed_at",
            _optional_utc(self.observed_at),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexManagedAuthority:
    """Provider-owned Codex home metadata without credential values."""

    authority_id: AuthorityId
    provider_identity: ProviderIdentity
    generation: AuthorityGeneration
    verified_at: datetime
    executable_version: str
    health: CredentialHealth
    kind: ClassVar[str] = "managed"

    def __post_init__(self) -> None:
        """Normalize verification time and bound executable metadata."""
        object.__setattr__(self, "verified_at", as_utc(self.verified_at))
        _require_bounded_text(
            self.executable_version,
            name="Codex executable version",
            maximum=_MAX_METADATA_BYTES,
        )


type CodexSubscriptionAuthority = CodexLegacyAuthority | CodexManagedAuthority


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexAccountAuthority:
    """One logical Codex account's subscription authority."""

    subscription: CodexSubscriptionAuthority
    provider_id: ClassVar[ProviderId] = ProviderId.CODEX


type AccountAuthority = ClaudeAccountAuthority | CodexAccountAuthority


@dataclass(frozen=True, slots=True, kw_only=True)
class SavedAccount:
    """Immutable secret-free account record keyed by stable ID."""

    account_id: SidekickAccountId
    label: AccountLabel
    provider_id: ProviderId
    plan: str
    authority: AccountAuthority
    credential_health: CredentialHealth
    last_refresh_at: datetime | None = None
    last_refresh_status: RefreshStatus | None = None
    last_refresh_error_code: str | None = None
    heartbeat_enabled: bool = False
    heartbeat_5h_reset_at: datetime | None = None
    heartbeat_window_resets: tuple[tuple[str, datetime], ...] | None = None
    heartbeat_targets: tuple[str, ...] | None = None
    last_heartbeat_at: datetime | None = None
    last_heartbeat_status: HeartbeatStatus | None = None
    last_heartbeat_error_code: str | None = None

    def __post_init__(self) -> None:
        """Validate provider ownership and normalize operational state."""
        if self.authority.provider_id is not self.provider_id:
            raise ValueError("Account authority provider does not match.")
        _require_bounded_text(
            self.plan,
            name="Account plan",
            maximum=_MAX_METADATA_BYTES,
        )
        object.__setattr__(
            self,
            "last_refresh_at",
            _optional_utc(self.last_refresh_at),
        )
        object.__setattr__(
            self,
            "heartbeat_5h_reset_at",
            _optional_utc(self.heartbeat_5h_reset_at),
        )
        object.__setattr__(
            self,
            "last_heartbeat_at",
            _optional_utc(self.last_heartbeat_at),
        )
        object.__setattr__(
            self,
            "last_refresh_error_code",
            _safe_metadata(
                self.last_refresh_error_code,
                name="Refresh error code",
            ),
        )
        object.__setattr__(
            self,
            "last_heartbeat_error_code",
            _safe_metadata(
                self.last_heartbeat_error_code,
                name="Heartbeat error code",
            ),
        )
        if self.heartbeat_window_resets is not None:
            normalized: list[tuple[str, datetime]] = []
            targets: set[str] = set()
            for target_id, reset_at in self.heartbeat_window_resets:
                _require_bounded_text(
                    target_id,
                    name="Heartbeat reset target",
                    maximum=_MAX_METADATA_BYTES,
                )
                if target_id in targets:
                    raise ValueError("Heartbeat reset targets must be unique.")
                targets.add(target_id)
                normalized.append((target_id, as_utc(reset_at)))
            object.__setattr__(
                self,
                "heartbeat_window_resets",
                tuple(normalized),
            )
        if self.heartbeat_targets is not None:
            for target_id in self.heartbeat_targets:
                _require_bounded_text(
                    target_id,
                    name="Heartbeat target",
                    maximum=_MAX_METADATA_BYTES,
                )
            if len(self.heartbeat_targets) != len(set(self.heartbeat_targets)):
                raise ValueError("Heartbeat targets must be unique.")

    def renamed(self, label: AccountLabel) -> SavedAccount:
        """Return this account with mutable label metadata replaced."""
        return replace(self, label=label)

    @property
    def has_managed_authority(self) -> bool:
        """Return whether any subscription authority is provider-managed."""
        subscription = self.authority.subscription
        return isinstance(
            subscription,
            ClaudeManagedLoginAuthority | CodexManagedAuthority,
        )


@dataclass(frozen=True, slots=True)
class AuthenticatedAccount[LeaseT]:
    """Worker-only account record paired with an operation-scoped lease."""

    account: SavedAccount
    lease: LeaseT = field(repr=False)


__all__ = [
    "AccountAuthority",
    "AuthenticatedAccount",
    "AuthorityGeneration",
    "AuthorityId",
    "ClaudeAccountAuthority",
    "ClaudeLegacyLoginAuthority",
    "ClaudeManagedLoginAuthority",
    "ClaudeSetupTokenAuthority",
    "CodexAccountAuthority",
    "CodexLegacyAuthority",
    "CodexManagedAuthority",
    "CredentialHealth",
    "MetricsFreshness",
    "ProviderIdentity",
    "SavedAccount",
    "SidekickAccountId",
]

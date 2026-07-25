"""Secret-free saved-account and credential-authority models."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import ClassVar

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialAction,
    CredentialHealth,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.accounts.validation import (
    MAX_METADATA_BYTES,
    require_bounded_text,
)
from sidekick_usages.core.expiry import Expiry, KnownExpiry, UnknownExpiry
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import (
    AccountLabel,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)

type ClaudeSubscriptionAuthority = (
    ClaudeStoredLoginAuthority | ClaudeManagedLoginAuthority
)
type CodexSubscriptionAuthority = CodexStoredAuthority | CodexManagedAuthority
type AccountAuthority = ClaudeAccountAuthority | CodexAccountAuthority


def _optional_utc(value: datetime | None) -> datetime | None:
    """Normalize an optional timestamp to aware UTC."""
    return None if value is None else as_utc(value)


def _safe_metadata(value: str | None, *, name: str) -> str | None:
    """Validate one optional non-secret metadata value."""
    if value is None:
        return None
    return require_bounded_text(
        value,
        name=name,
        maximum=MAX_METADATA_BYTES,
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
class ClaudeStoredLoginAuthority:
    """Reference-only metadata for a Sidekick-stored Claude login."""

    authority_id: AuthorityId
    provider_identity: ProviderIdentity | None
    access_expires_at: datetime | None
    refresh_expires_at: datetime | None
    health: CredentialHealth
    observed_at: datetime | None = None
    kind: ClassVar[str] = "stored"

    def __post_init__(self) -> None:
        """Normalize stored login timestamps."""
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
    access_expires_at: datetime
    refresh_expires_at: datetime | None
    verified_at: datetime
    executable_version: str
    health: CredentialHealth
    action: CredentialAction
    kind: ClassVar[str] = "managed"

    def __post_init__(self) -> None:
        """Normalize verification time and bound executable metadata."""
        object.__setattr__(
            self,
            "access_expires_at",
            as_utc(self.access_expires_at),
        )
        object.__setattr__(
            self,
            "refresh_expires_at",
            _optional_utc(self.refresh_expires_at),
        )
        object.__setattr__(self, "verified_at", as_utc(self.verified_at))
        require_bounded_text(
            self.executable_version,
            name="Claude executable version",
            maximum=MAX_METADATA_BYTES,
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
class CodexStoredAuthority:
    """Reference-only metadata for a Sidekick-stored Codex login."""

    authority_id: AuthorityId
    provider_identity: ProviderIdentity | None
    expires_at: datetime | None
    generation: AuthorityGeneration | None
    health: CredentialHealth
    observed_at: datetime | None = None
    kind: ClassVar[str] = "stored"

    def __post_init__(self) -> None:
        """Normalize stored Codex timestamps."""
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
        require_bounded_text(
            self.executable_version,
            name="Codex executable version",
            maximum=MAX_METADATA_BYTES,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexAccountAuthority:
    """One logical Codex account's subscription authority."""

    subscription: CodexSubscriptionAuthority
    provider_id: ClassVar[ProviderId] = ProviderId.CODEX


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
    heartbeat_window_resets: tuple[tuple[str, datetime], ...] | None = None
    heartbeat_targets: tuple[str, ...] | None = None
    last_heartbeat_at: datetime | None = None
    last_heartbeat_status: HeartbeatStatus | None = None
    last_heartbeat_error_code: str | None = None

    def __post_init__(self) -> None:
        """Validate provider ownership and normalize operational state."""
        if self.authority.provider_id is not self.provider_id:
            raise ValueError("Account authority provider does not match.")
        require_bounded_text(
            self.plan,
            name="Account plan",
            maximum=MAX_METADATA_BYTES,
        )
        object.__setattr__(
            self,
            "last_refresh_at",
            _optional_utc(self.last_refresh_at),
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
                require_bounded_text(
                    target_id,
                    name="Heartbeat reset target",
                    maximum=MAX_METADATA_BYTES,
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
                require_bounded_text(
                    target_id,
                    name="Heartbeat target",
                    maximum=MAX_METADATA_BYTES,
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

    @property
    def provider_identity(self) -> ProviderIdentity | None:
        """Return the non-renderable provider identity when established."""
        authority = self.authority
        if isinstance(authority, ClaudeAccountAuthority):
            subscription = authority.subscription
            return (
                subscription.provider_identity
                if isinstance(
                    subscription,
                    ClaudeStoredLoginAuthority | ClaudeManagedLoginAuthority,
                )
                else None
            )
        return authority.subscription.provider_identity

    @property
    def access_expiry(self) -> Expiry:
        """Return secret-free access-expiry metadata for maintenance."""
        authority = self.authority
        if isinstance(authority, ClaudeAccountAuthority):
            subscription = authority.subscription
            if isinstance(subscription, ClaudeStoredLoginAuthority):
                return (
                    KnownExpiry(subscription.access_expires_at)
                    if subscription.access_expires_at is not None
                    else UnknownExpiry()
                )
            if isinstance(subscription, ClaudeManagedLoginAuthority):
                return KnownExpiry(subscription.access_expires_at)
            setup = authority.setup_token
            return (
                KnownExpiry(setup.expires_at)
                if setup is not None and setup.expires_at is not None
                else UnknownExpiry()
            )
        subscription = authority.subscription
        if isinstance(subscription, CodexStoredAuthority):
            return (
                KnownExpiry(subscription.expires_at)
                if subscription.expires_at is not None
                else UnknownExpiry()
            )
        return UnknownExpiry()

    @property
    def refresh_expiry(self) -> Expiry:
        """Return secret-free refresh-authority lifetime metadata."""
        authority = self.authority
        if isinstance(authority, ClaudeAccountAuthority):
            subscription = authority.subscription
            if not isinstance(
                subscription,
                ClaudeStoredLoginAuthority | ClaudeManagedLoginAuthority,
            ):
                return UnknownExpiry()
            expires_at = subscription.refresh_expires_at
            return (
                KnownExpiry(expires_at)
                if expires_at is not None
                else UnknownExpiry()
            )
        return UnknownExpiry()


@dataclass(frozen=True, slots=True)
class AuthenticatedAccount[LeaseType]:
    """Worker-only account record paired with an operation-scoped lease."""

    account: SavedAccount
    lease: LeaseType = field(repr=False)

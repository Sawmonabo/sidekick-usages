"""Read-only diagnostics over secret-free saved accounts."""

from collections.abc import Collection, Sequence
from datetime import datetime
from typing import assert_never

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    ClaudeSetupTokenAuthority,
    ClaudeStoredLoginAuthority,
    CodexAccountAuthority,
    CodexManagedAuthority,
    CodexStoredAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    CredentialAction,
    CredentialHealth,
)
from sidekick_usages.core.expiry import (
    ClassifiedExpiry,
    ExpiredExpiry,
    InvalidExpiry,
    KnownExpiry,
    UnknownExpiry,
    ValidExpiry,
    classify_expiry,
)
from sidekick_usages.core.selection.types import (
    AuthorityGenerationRelation,
)
from sidekick_usages.core.types import (
    ExitCode,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
    highest_exit_code,
)
from sidekick_usages.credentials.capabilities.ports import (
    ProviderCapabilityEvidenceSource,
)
from sidekick_usages.credentials.claude.lifetime import (
    ClaudeLoginRenewalState,
    classify_saved_claude_login_renewal,
)
from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.daemon.types.lifecycle import ServiceComponentState
from sidekick_usages.doctor.accounts.models import (
    AccountDiagnostic,
    AuthorityDiagnostic,
    DoctorAuthorityManagement,
    DoctorCredentialKind,
    DoctorFailedResult,
    DoctorReadyResult,
    DoctorResult,
    HeartbeatSupport,
    IdentityState,
)
from sidekick_usages.doctor.runtime.models import (
    ScheduledOperationDiagnostic,
    UnfinishedActivationDiagnostic,
)
from sidekick_usages.doctor.runtime.service import DoctorRuntimeService
from sidekick_usages.doctor.runtime.types import (
    DoctorAccountWarning,
    NativeAccountRelation,
)
from sidekick_usages.persistence.credentials.refresh.artifacts import (
    CredentialRefreshStateKind,
)
from sidekick_usages.persistence.errors import (
    exit_code_for_persistence_code,
)

_CLAUDE_SETUP_USAGE_ROUTE = "/v1/messages headers"
_CLAUDE_SUBSCRIPTION_USAGE_ROUTE = "/api/oauth/usage"
_CODEX_USAGE_ROUTE = "/backend-api/codex/usage"
_SECONDS_PER_HOUR = 3_600
_SECONDS_PER_DAY = 86_400
_ACTION_REQUIRED_HEALTH = frozenset(
    {
        CredentialHealth.LOGIN_REQUIRED,
        CredentialHealth.UNREADABLE,
        CredentialHealth.MALFORMED,
        CredentialHealth.UNSUPPORTED,
        CredentialHealth.RECONCILIATION_REQUIRED,
    }
)
_PROVEN_HEARTBEAT_STATUSES = frozenset(
    {
        HeartbeatStatus.ACTIVE,
        HeartbeatStatus.WARMED,
    }
)
_MANUAL_SERVICE_STATES = frozenset(
    {
        ServiceComponentState.ABSENT,
        ServiceComponentState.FEATURE_DISABLED,
    }
)
_FAILED_SERVICE_STATES = frozenset(
    {
        ServiceComponentState.UNAVAILABLE,
        ServiceComponentState.UNHEALTHY,
    }
)


class DoctorService:
    """Build secret-safe diagnostics for every saved account."""

    def __init__(
        self,
        accounts: Sequence[SavedAccount],
        capabilities: ProviderCapabilityEvidenceSource,
        heartbeat_provider_ids: Collection[ProviderId],
        clock: Clock,
        runtime: DoctorRuntimeService,
    ) -> None:
        """:param accounts: Validated secret-free account snapshot.

        :param capabilities: Cached provider capability evidence.
        :param heartbeat_provider_ids: Registered heartbeat identifiers.
        :param clock: Aware UTC application wall clock.
        :param runtime: Cached native relation and metrics diagnostics.
        """
        self.accounts = tuple(accounts)
        self._capabilities = capabilities
        self._heartbeat_provider_ids = frozenset(heartbeat_provider_ids)
        self._clock = clock
        self._runtime = runtime

    def diagnostics(
        self,
        *,
        provider_id: ProviderId | None = None,
        label: str | None = None,
    ) -> list[AccountDiagnostic]:
        """Return diagnostics matching optional provider and label filters."""
        reference_time = self._clock.now()
        return [
            self._diagnostic(account, reference_time)
            for account in self.accounts
            if (provider_id is None or account.provider_id is provider_id)
            and (label is None or account.label == label)
        ]

    def scheduled_operations(
        self,
        *,
        provider_id: ProviderId | None = None,
        label: str | None = None,
    ) -> tuple[ScheduledOperationDiagnostic, ...]:
        """Return durable work matching optional account filters."""
        return tuple(
            operation
            for operation in self._runtime.operations
            if (provider_id is None or operation.provider_id is provider_id)
            and (
                label is None
                or (
                    operation.account_label is not None
                    and operation.account_label == label
                )
            )
        )

    def unfinished_activations(
        self,
        *,
        provider_id: ProviderId | None = None,
        label: str | None = None,
    ) -> tuple[UnfinishedActivationDiagnostic, ...]:
        """Return unfinished activations matching optional filters."""
        return tuple(
            activation
            for activation in self._runtime.unfinished_activations
            if (provider_id is None or activation.provider_id is provider_id)
            and (label is None or activation.target_label == label)
        )

    def _diagnostic(
        self,
        account: SavedAccount,
        reference_time: datetime,
    ) -> AccountDiagnostic:
        """Build one logical account diagnostic."""
        provider_available = self._capabilities.ready(account.provider_id)
        setup_token, subscription = _authority_diagnostics(
            account,
            reference_time,
            provider_available=provider_available,
        )
        identity_state = _identity_state(account)
        heartbeat_support = _heartbeat_support(
            account,
            self._heartbeat_provider_ids,
        )
        runtime = self._runtime.diagnostic(account.account_id)
        warning = _account_warning(account, runtime.native_relation)
        manual_action = _manual_action(
            account,
            identity_state,
            setup_token,
            subscription,
            runtime.native_relation,
            runtime.selected_generation_relation,
            warning,
        )
        return AccountDiagnostic(
            label=account.label,
            provider=account.provider_id,
            provider_available=provider_available,
            plan=account.plan,
            credential_health=account.credential_health,
            identity_state=identity_state,
            setup_token=setup_token,
            subscription=subscription,
            last_refresh_at=account.last_refresh_at,
            last_refresh_status=account.last_refresh_status,
            last_refresh_error=account.last_refresh_error_code,
            heartbeat_support=heartbeat_support,
            heartbeat_enabled=account.heartbeat_enabled,
            heartbeat=_heartbeat_label(account, heartbeat_support),
            heartbeat_window_resets=account.heartbeat_window_resets,
            heartbeat_targets=account.heartbeat_targets,
            last_heartbeat_at=account.last_heartbeat_at,
            last_heartbeat_status=account.last_heartbeat_status,
            last_heartbeat_error=account.last_heartbeat_error_code,
            native_relation=runtime.native_relation,
            selected_generation_relation=(
                runtime.selected_generation_relation
            ),
            metrics_freshness=runtime.metrics_freshness,
            metrics_observed_at=runtime.metrics_observed_at,
            warning=warning,
            manual_action=manual_action,
        )


def doctor_exit_code(
    result: DoctorResult,
) -> ExitCode:
    """Reduce every completed Doctor evidence channel by precedence."""
    account_code = ExitCode.SUCCESS
    persistence_code = ExitCode.SUCCESS
    if isinstance(result, DoctorReadyResult):
        if any(
            diagnostic.manual_action is not None
            for diagnostic in result.diagnostics
        ):
            account_code = ExitCode.MANUAL_ACTION
    elif isinstance(result, DoctorFailedResult):
        persistence_code = exit_code_for_persistence_code(result.failure.code)
    else:
        assert_never(result)
    capability_code = (
        ExitCode.SYSTEM_ERROR
        if any(
            not capability.ready for capability in result.capabilities.results
        )
        else ExitCode.SUCCESS
    )
    refresh_code = (
        ExitCode.SYSTEM_ERROR
        if (
            isinstance(result, DoctorReadyResult)
            and result.refresh_state.kind
            is not CredentialRefreshStateKind.CLEAN
        )
        else ExitCode.SUCCESS
    )
    return highest_exit_code(
        _supervisor_exit_code(result.supervisor),
        capability_code,
        refresh_code,
        persistence_code,
        account_code,
    )


def _supervisor_exit_code(health: SupervisorHealth) -> ExitCode:
    """Reduce independent resident-service evidence truthfully."""
    primary = (health.platform, health.process)
    if any(state in _FAILED_SERVICE_STATES for state in primary):
        return ExitCode.SCHEDULER_ERROR
    if any(state in _MANUAL_SERVICE_STATES for state in primary):
        return ExitCode.MANUAL_ACTION
    remaining = (
        health.rescue,
        health.socket,
        health.peer,
        health.protocol,
        health.queue,
        health.journal,
        health.broker,
    )
    if any(
        state in _FAILED_SERVICE_STATES or state in _MANUAL_SERVICE_STATES
        for state in remaining
    ):
        return ExitCode.SCHEDULER_ERROR
    return ExitCode.SUCCESS


def _manual_action(
    account: SavedAccount,
    identity_state: IdentityState,
    setup_token: AuthorityDiagnostic | None,
    subscription: AuthorityDiagnostic | None,
    native_relation: NativeAccountRelation,
    generation_relation: AuthorityGenerationRelation,
    warning: DoctorAccountWarning | None,
) -> tuple[str, ...] | None:
    """Return one exact command for the highest-priority account repair."""
    label = str(account.label)
    if not account.has_managed_authority:
        return ("sidekick-usages", "migrate", "managed-auth")
    if identity_state is IdentityState.ASSOCIATION_REQUIRED:
        return (
            "sidekick-usages",
            "refresh",
            label,
            "--provider",
            ProviderId.CLAUDE.value,
            "--replace-identity",
        )
    if setup_token is not None and setup_token.manual_action_required:
        return (
            "sidekick-usages",
            "claude",
            "setup-token",
            "--label",
            label,
            "--force",
        )
    if (
        (subscription is not None and subscription.manual_action_required)
        or account.last_refresh_status is RefreshStatus.FAILED
        or (warning is DoctorAccountWarning.LOGIN_REQUIRED)
    ):
        return _login_action(account)
    if warning is DoctorAccountWarning.RECONCILIATION_REQUIRED or (
        account.provider_id is ProviderId.CODEX
        and native_relation is NativeAccountRelation.ACTIVE
        and generation_relation is AuthorityGenerationRelation.OLDER
    ):
        return (
            "sidekick-usages",
            "use",
            account.provider_id.value,
            label,
        )
    return (
        _login_action(account)
        if (
            account.provider_id is ProviderId.CODEX
            and native_relation is NativeAccountRelation.ACTIVE
            and generation_relation is AuthorityGenerationRelation.NEWER
        )
        else None
    )


def _login_action(account: SavedAccount) -> tuple[str, ...]:
    """Return the provider-owned official login command for one account."""
    label = str(account.label)
    if account.provider_id is ProviderId.CODEX:
        return ("sidekick-usages", "codex", "login", label)
    return (
        "sidekick-usages",
        "refresh",
        label,
        "--provider",
        ProviderId.CLAUDE.value,
    )


def _account_warning(
    account: SavedAccount,
    native_relation: NativeAccountRelation,
) -> DoctorAccountWarning | None:
    """Select one account-specific warning without persistent badge state."""
    if (
        native_relation is NativeAccountRelation.RECONCILIATION_REQUIRED
        or account.credential_health
        is CredentialHealth.RECONCILIATION_REQUIRED
    ):
        return DoctorAccountWarning.RECONCILIATION_REQUIRED
    if account.credential_health in {
        CredentialHealth.LOGIN_REQUIRED,
        CredentialHealth.MALFORMED,
        CredentialHealth.UNREADABLE,
    }:
        return DoctorAccountWarning.LOGIN_REQUIRED
    return None


def _authority_diagnostics(
    account: SavedAccount,
    reference_time: datetime,
    *,
    provider_available: bool,
) -> tuple[AuthorityDiagnostic | None, AuthorityDiagnostic | None]:
    """Classify each independent authority on one logical account."""
    authority = account.authority
    if isinstance(authority, ClaudeAccountAuthority):
        setup_token = (
            _setup_token_diagnostic(
                authority.setup_token,
                reference_time,
            )
            if authority.setup_token is not None
            else None
        )
        subscription = (
            _claude_subscription_diagnostic(
                account,
                authority.subscription,
                reference_time,
                provider_available=provider_available,
            )
            if authority.subscription is not None
            else None
        )
        return setup_token, subscription
    if isinstance(authority, CodexAccountAuthority):
        return (
            None,
            _codex_subscription_diagnostic(
                authority.subscription,
                reference_time,
                provider_available=provider_available,
            ),
        )
    assert_never(authority)


def _setup_token_diagnostic(
    authority: ClaudeSetupTokenAuthority,
    reference_time: datetime,
) -> AuthorityDiagnostic:
    """Classify one fixed-lifetime setup-token authority."""
    access_expiry = _classify_time(authority.expires_at, reference_time)
    refresh_expiry = UnknownExpiry()
    manual_action_required = (
        authority.health in _ACTION_REQUIRED_HEALTH
        or authority.health is CredentialHealth.REFRESH_DUE
        or isinstance(access_expiry, ExpiredExpiry | InvalidExpiry)
    )
    return AuthorityDiagnostic(
        kind=DoctorCredentialKind.SETUP_TOKEN,
        management=DoctorAuthorityManagement.SIDEKICK_STORED,
        health=authority.health,
        usage_route=_CLAUDE_SETUP_USAGE_ROUTE,
        access_expires_at=_expiry_time(access_expiry),
        access_expiry_state=access_expiry.state,
        access_expiry_display=_access_expiry_display(
            access_expiry,
            reference_time,
        ),
        refresh_expires_at=None,
        refresh_expiry_state=refresh_expiry.state,
        refresh_expiry_display="unavailable",
        login_renewal_state=ClaudeLoginRenewalState.NOT_APPLICABLE,
        provider_action=None,
        can_auto_refresh=False,
        manual_action_required=manual_action_required,
    )


def _claude_subscription_diagnostic(
    account: SavedAccount,
    authority: ClaudeStoredLoginAuthority | ClaudeManagedLoginAuthority,
    reference_time: datetime,
    *,
    provider_available: bool,
) -> AuthorityDiagnostic:
    """Classify one stored or provider-managed Claude login."""
    access_expiry = _classify_time(
        authority.access_expires_at,
        reference_time,
    )
    refresh_expiry = _classify_time(
        authority.refresh_expires_at,
        reference_time,
    )
    provider_action = (
        authority.action
        if isinstance(authority, ClaudeManagedLoginAuthority)
        else None
    )
    management = (
        DoctorAuthorityManagement.PROVIDER_MANAGED
        if isinstance(authority, ClaudeManagedLoginAuthority)
        else DoctorAuthorityManagement.SIDEKICK_STORED
    )
    can_auto_refresh = _can_auto_refresh(
        provider_available=provider_available,
        health=authority.health,
        provider_action=provider_action,
        refresh_expiry=refresh_expiry,
    )
    renewal_state = classify_saved_claude_login_renewal(
        account,
        reference_time=reference_time,
    )
    return AuthorityDiagnostic(
        kind=DoctorCredentialKind.SUBSCRIPTION_LOGIN,
        management=management,
        health=authority.health,
        usage_route=_CLAUDE_SUBSCRIPTION_USAGE_ROUTE,
        access_expires_at=_expiry_time(access_expiry),
        access_expiry_state=access_expiry.state,
        access_expiry_display=_access_expiry_display(
            access_expiry,
            reference_time,
        ),
        refresh_expires_at=_expiry_time(refresh_expiry),
        refresh_expiry_state=refresh_expiry.state,
        refresh_expiry_display=_refresh_expiry_display(refresh_expiry),
        login_renewal_state=renewal_state,
        provider_action=provider_action,
        can_auto_refresh=can_auto_refresh,
        manual_action_required=_login_action_required(
            health=authority.health,
            provider_action=provider_action,
            access_expiry=access_expiry,
            refresh_expiry=refresh_expiry,
            renewal_state=renewal_state,
            can_auto_refresh=can_auto_refresh,
        ),
    )


def _codex_subscription_diagnostic(
    authority: CodexStoredAuthority | CodexManagedAuthority,
    reference_time: datetime,
    *,
    provider_available: bool,
) -> AuthorityDiagnostic:
    """Classify one stored or provider-managed Codex login."""
    expires_at = (
        authority.expires_at
        if isinstance(authority, CodexStoredAuthority)
        else None
    )
    access_expiry = _classify_time(expires_at, reference_time)
    refresh_expiry = UnknownExpiry()
    management = (
        DoctorAuthorityManagement.PROVIDER_MANAGED
        if isinstance(authority, CodexManagedAuthority)
        else DoctorAuthorityManagement.SIDEKICK_STORED
    )
    can_auto_refresh = _can_auto_refresh(
        provider_available=provider_available,
        health=authority.health,
        provider_action=None,
        refresh_expiry=refresh_expiry,
    )
    return AuthorityDiagnostic(
        kind=DoctorCredentialKind.CODEX_LOGIN,
        management=management,
        health=authority.health,
        usage_route=_CODEX_USAGE_ROUTE,
        access_expires_at=_expiry_time(access_expiry),
        access_expiry_state=access_expiry.state,
        access_expiry_display=_access_expiry_display(
            access_expiry,
            reference_time,
        ),
        refresh_expires_at=None,
        refresh_expiry_state=refresh_expiry.state,
        refresh_expiry_display="unavailable",
        login_renewal_state=ClaudeLoginRenewalState.NOT_APPLICABLE,
        provider_action=None,
        can_auto_refresh=can_auto_refresh,
        manual_action_required=_login_action_required(
            health=authority.health,
            provider_action=None,
            access_expiry=access_expiry,
            refresh_expiry=refresh_expiry,
            renewal_state=ClaudeLoginRenewalState.NOT_APPLICABLE,
            can_auto_refresh=can_auto_refresh,
        ),
    )


def _identity_state(account: SavedAccount) -> IdentityState:
    """Return identity availability without rendering its value."""
    authority = account.authority
    if isinstance(authority, ClaudeAccountAuthority):
        subscription = authority.subscription
        if subscription is None:
            return IdentityState.ASSOCIATION_REQUIRED
        return (
            IdentityState.KNOWN
            if subscription.provider_identity is not None
            else IdentityState.UNAVAILABLE
        )
    if isinstance(authority, CodexAccountAuthority):
        return (
            IdentityState.KNOWN
            if authority.subscription.provider_identity is not None
            else IdentityState.UNAVAILABLE
        )
    assert_never(authority)


def _classify_time(
    expires_at: datetime | None,
    reference_time: datetime,
) -> ClassifiedExpiry:
    """Classify optional expiry metadata against one wall time."""
    expiry = (
        KnownExpiry(expires_at) if expires_at is not None else UnknownExpiry()
    )
    return classify_expiry(expiry, now=reference_time)


def _expiry_time(expiry: ClassifiedExpiry) -> datetime | None:
    """Return an authoritative classified expiry time when available."""
    if isinstance(expiry, ValidExpiry | ExpiredExpiry):
        return expiry.at
    return None


def _access_expiry_display(
    expiry: ClassifiedExpiry,
    reference_time: datetime,
) -> str:
    """Return concise human access-expiry copy."""
    if isinstance(expiry, InvalidExpiry):
        return "invalid"
    if isinstance(expiry, ExpiredExpiry):
        return "expired"
    if not isinstance(expiry, ValidExpiry):
        return "unavailable"
    seconds = int((expiry.at - reference_time).total_seconds())
    if seconds < _SECONDS_PER_HOUR:
        return f"in {seconds // 60}m"
    if seconds < _SECONDS_PER_DAY:
        hours, minutes = divmod(seconds // 60, 60)
        return f"in {hours}h {minutes}m"
    days, remainder = divmod(seconds, _SECONDS_PER_DAY)
    return f"in {days}d {remainder // _SECONDS_PER_HOUR}h"


def _refresh_expiry_display(expiry: ClassifiedExpiry) -> str:
    """Return human login-lifetime copy."""
    if isinstance(expiry, InvalidExpiry):
        return "invalid"
    if not isinstance(expiry, ValidExpiry | ExpiredExpiry):
        return "unavailable"
    local = expiry.at.astimezone()
    date = local.strftime("%b ") + str(local.day) + local.strftime(", %Y")
    return f"{date} (expired)" if isinstance(expiry, ExpiredExpiry) else date


def _can_auto_refresh(
    *,
    provider_available: bool,
    health: CredentialHealth,
    provider_action: CredentialAction | None,
    refresh_expiry: ClassifiedExpiry,
) -> bool:
    """Return whether saved metadata permits automatic refresh."""
    return (
        provider_available
        and health not in _ACTION_REQUIRED_HEALTH
        and provider_action is not CredentialAction.LOGIN
        and not isinstance(refresh_expiry, ExpiredExpiry | InvalidExpiry)
    )


def _login_action_required(
    *,
    health: CredentialHealth,
    provider_action: CredentialAction | None,
    access_expiry: ClassifiedExpiry,
    refresh_expiry: ClassifiedExpiry,
    renewal_state: ClaudeLoginRenewalState,
    can_auto_refresh: bool,
) -> bool:
    """Return whether one login authority needs user intervention."""
    renewal_required = renewal_state in {
        ClaudeLoginRenewalState.RENEWAL_DUE,
        ClaudeLoginRenewalState.EXPIRED,
        ClaudeLoginRenewalState.INVALID,
    }
    return (
        health in _ACTION_REQUIRED_HEALTH
        or provider_action is CredentialAction.LOGIN
        or isinstance(access_expiry, InvalidExpiry)
        or isinstance(refresh_expiry, ExpiredExpiry | InvalidExpiry)
        or renewal_required
        or (health is CredentialHealth.REFRESH_DUE and not can_auto_refresh)
        or (isinstance(access_expiry, ExpiredExpiry) and not can_auto_refresh)
    )


def _heartbeat_support(
    account: SavedAccount,
    heartbeat_provider_ids: frozenset[ProviderId],
) -> HeartbeatSupport:
    """Return heartbeat support without opening credential material."""
    if account.provider_id not in heartbeat_provider_ids:
        return HeartbeatSupport.UNSUPPORTED
    if account.last_heartbeat_status is HeartbeatStatus.UNSUPPORTED:
        return HeartbeatSupport.UNSUPPORTED
    if account.last_heartbeat_status in _PROVEN_HEARTBEAT_STATUSES:
        return HeartbeatSupport.SUPPORTED
    authority = account.authority
    if isinstance(authority, CodexAccountAuthority):
        return HeartbeatSupport.SUPPORTED
    if isinstance(authority, ClaudeAccountAuthority):
        if authority.setup_token is not None:
            return HeartbeatSupport.SUPPORTED
        return HeartbeatSupport.UNKNOWN
    assert_never(authority)


def _heartbeat_label(
    account: SavedAccount,
    support: HeartbeatSupport,
) -> str:
    """Return a compact heartbeat state for doctor output."""
    if support is HeartbeatSupport.UNSUPPORTED:
        return "unsupported"
    if account.last_heartbeat_status is HeartbeatStatus.FAILED:
        return "needs-login"
    if support is HeartbeatSupport.UNKNOWN:
        return "unknown"
    return "on" if account.heartbeat_enabled else "off"

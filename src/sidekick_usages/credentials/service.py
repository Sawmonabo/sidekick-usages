"""Provider-neutral credential coordination."""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import InvalidExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    Credentials,
    DetectedCredentials,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials.account_state import (
    persist_provider_plan_without_credentials,
)
from sidekick_usages.credentials.claude.setup_save import (
    preview_claude_setup_token_save,
)
from sidekick_usages.credentials.claude.transitions import (
    apply_claude_transition,
)
from sidekick_usages.credentials.codex.migration import (
    CodexAuthMigrationCoordinator,
)
from sidekick_usages.credentials.codex.types import CodexLoginEventSink
from sidekick_usages.credentials.models import (
    ClaudeSetupTokenSavePreview,
    CredentialLoginResult,
    CredentialRefreshResult,
    CredentialRefreshSuccess,
    CredentialSaveResult,
    CredentialSaveSuccess,
    CredentialSource,
    CredentialUpdateResult,
    CredentialUpdateSuccess,
    LocalCredentialSource,
    TokenPromptSpec,
)
from sidekick_usages.credentials.refresh import (
    CredentialRefreshCoordinator,
    CredentialRefreshReason,
)
from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    RateLimitError,
    TransientError,
)
from sidekick_usages.http.client import HttpClient
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import SourceChangedError
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
)

_CLAUDE_SETUP_HINT = (
    "Run `sidekick-usages setup-token claude` to generate one."
)


def _failure(
    provider_id: ProviderId,
    kind: ProviderFailureKind,
    message: str,
) -> ProviderFailure:
    return ProviderFailure(
        provider_id=provider_id,
        kind=kind,
        message=message,
    )


def _copy_account(
    account: Account,
    *,
    label: AccountLabel | None = None,
    credentials: Credentials | None = None,
) -> Account:
    resets = account.heartbeat_window_resets
    return replace(
        account,
        label=label or account.label,
        credentials=credentials or account.credentials,
        heartbeat_window_resets=(
            dict(resets.items()) if resets is not None else None
        ),
    )


@dataclass(frozen=True, slots=True)
class _SavePlan:
    candidate: Account
    previous: Account | None
    created: bool
    previous_label: str | None


class CredentialService:
    """Coordinate provider credentials and durable account authority."""

    def __init__(
        self,
        store: AccountStore,
        http: HttpClient,
        providers: Mapping[ProviderId, Provider],
        *,
        clock: Clock,
        refresh_coordinator: CredentialRefreshCoordinator | None = None,
        codex_auth_migration: CodexAuthMigrationCoordinator | None = None,
    ) -> None:
        """Bind credential workflows to invocation-scoped dependencies.

        :param store: Loaded transactional account store.
        :param http: Shared provider HTTP facade.
        :param providers: Closed provider adapter registry.
        :param clock: Aware application wall clock.
        """
        self._store = store
        self._http = http
        self._providers = providers
        self._clock = clock
        self._refresh = refresh_coordinator
        self._codex_auth_migration = codex_auth_migration

    def prompt_spec(
        self,
        provider_id: ProviderId,
    ) -> TokenPromptSpec | ProviderFailure:
        """Return safe token-entry metadata without exposing an adapter."""
        provider = self._providers.get(provider_id)
        if provider is None:
            return _failure(
                provider_id,
                ProviderFailureKind.UNSUPPORTED,
                f"Provider '{provider_id}' is not registered.",
            )
        try:
            return TokenPromptSpec(
                provider_id=provider.id,
                display_name=provider.display_name,
                token_pattern=provider.token_pattern,
                setup_hint=(
                    _CLAUDE_SETUP_HINT
                    if provider.id is ProviderId.CLAUDE
                    else None
                ),
            )
        except TypeError, UnicodeError, ValueError:
            return _failure(
                provider_id,
                ProviderFailureKind.MALFORMED,
                "Provider token prompt metadata is invalid.",
            )

    def resolve(self, source: CredentialSource) -> CredentialDetection:
        """Resolve one source through its owning provider boundary."""
        provider = self._providers.get(source.provider_id)
        if provider is None:
            return _failure(
                source.provider_id,
                ProviderFailureKind.UNSUPPORTED,
                f"Provider '{source.provider_id}' is not registered.",
            )
        try:
            if isinstance(source, LocalCredentialSource):
                home = (
                    source.credential_home.expanduser()
                    if source.credential_home is not None
                    else None
                )
                result = provider.detect_credentials(home)
            else:
                result = provider.credentials_from_token(source.token)
        except ProviderBoundaryError as error:
            return error.failure
        if (
            isinstance(result, DetectedCredentials)
            and result.provider_id is not source.provider_id
        ):
            return _failure(
                source.provider_id,
                ProviderFailureKind.MALFORMED,
                "The provider returned incompatible credentials.",
            )
        return result

    def save(
        self,
        source: CredentialSource,
        *,
        label: AccountLabel | None,
        plan: str | None,
        force: bool,
        replace_identity: bool = False,
    ) -> CredentialSaveResult:
        """Resolve and durably save one account in a single commit."""
        result = self.resolve(source)
        if isinstance(result, ProviderFailure):
            return result
        if isinstance(result.expiry, InvalidExpiry):
            return _failure(
                source.provider_id,
                ProviderFailureKind.MALFORMED,
                "Detected credential expiry metadata is invalid.",
            )
        save_plan = self._build_save_plan(
            source,
            result,
            label=label,
            plan=plan,
            force=force,
            replace_identity=replace_identity,
        )
        if isinstance(save_plan, ProviderFailure):
            return save_plan
        candidate = save_plan.candidate
        previous = save_plan.previous

        warning: str | None = None
        if save_plan.created or (
            previous is not None
            and (
                previous.access_token != candidate.access_token
                or type(previous.credentials)
                is not type(candidate.credentials)
            )
        ):
            validation = self._validate_new_account(
                candidate,
                self._providers[source.provider_id],
            )
            if isinstance(validation, ProviderFailure):
                return validation
            warning = validation

        self._store.persist_credentials(
            candidate,
            previous_label=save_plan.previous_label,
        )
        return CredentialSaveSuccess(
            candidate.label,
            save_plan.created,
            warning,
        )

    def _build_save_plan(
        self,
        source: CredentialSource,
        detected: DetectedCredentials,
        *,
        label: AccountLabel | None,
        plan: str | None,
        force: bool,
        replace_identity: bool,
    ) -> _SavePlan | ProviderFailure:
        """Build one save candidate without mutating durable state."""
        by_token = self._store.find_by_token(
            source.provider_id,
            detected.access_token,
        )
        target_label = label or (
            by_token.label
            if by_token is not None
            else self._store.generate_label(
                source.provider_id,
                plan or detected.plan or "account",
            )
        )
        target = self._store.get(str(target_label))
        if (
            by_token is not None
            and target is not None
            and target.label != by_token.label
        ):
            return _failure(
                source.provider_id,
                ProviderFailureKind.UNSUPPORTED,
                "The requested label is already used by another account; "
                "remove it before moving this saved token.",
            )
        if by_token is None and target is not None and not force:
            return _failure(
                source.provider_id,
                ProviderFailureKind.REJECTED,
                f"Account '{target_label}' already exists; use --force.",
            )
        previous = by_token or target
        replacing = target is not None and by_token is None
        if previous is None or (
            replacing
            and not isinstance(
                detected.credentials,
                ClaudeSetupTokenCredentials,
            )
        ):
            candidate = Account(
                label=target_label,
                credentials=detected.credentials,
                plan=plan or detected.plan or "unknown",
            )
        else:
            candidate = _copy_account(previous, label=target_label)
            applied = self._apply_detected(
                candidate,
                detected,
                replace_identity=replace_identity,
                replace_auth_method=force,
            )
            if isinstance(applied, ProviderFailure):
                return applied
            candidate = applied
            if plan is not None:
                candidate.plan = plan
            if (
                candidate.provider_id is ProviderId.CLAUDE
                and previous.credentials != candidate.credentials
            ):
                candidate.last_refresh_at = None
                candidate.last_refresh_status = None
                candidate.last_refresh_error = None
        return _SavePlan(
            candidate,
            previous,
            previous is None,
            (
                str(by_token.label)
                if by_token is not None and by_token.label != target_label
                else None
            ),
        )

    def preview_setup_token_save(
        self,
        label: AccountLabel | None,
        *,
        force: bool,
        replace_identity: bool,
    ) -> ClaudeSetupTokenSavePreview | ProviderFailure | None:
        """Authorize a known login-to-setup crossing before token capture."""
        return preview_claude_setup_token_save(
            self._store,
            label,
            force=force,
            replace_identity=replace_identity,
        )

    def refresh_claude_from_source(
        self,
        label: str,
        source: LocalCredentialSource,
        *,
        replace_identity: bool,
        replace_auth_method: bool = False,
    ) -> CredentialRefreshResult:
        """Import one local Claude login into an existing saved account."""
        if source.provider_id is not ProviderId.CLAUDE:
            return _failure(
                source.provider_id,
                ProviderFailureKind.UNSUPPORTED,
                "Local-login import is supported only for Claude.",
            )
        account = self._store.get(label, provider_id=ProviderId.CLAUDE)
        if account is None:
            return _failure(
                source.provider_id,
                ProviderFailureKind.MISSING,
                f"No account named '{label}'.",
            )
        detected = self.resolve(source)
        if isinstance(detected, ProviderFailure):
            return detected
        candidate = _copy_account(account)
        applied = self._apply_detected(
            candidate,
            detected,
            replace_identity=replace_identity,
            replace_auth_method=replace_auth_method,
        )
        if isinstance(applied, ProviderFailure):
            return applied
        candidate = applied
        reference_time = self._clock.now()
        candidate.last_refresh_at = reference_time
        candidate.last_refresh_status = RefreshStatus.OK
        candidate.last_refresh_error = None
        self._store.persist_credentials(candidate)
        return CredentialRefreshSuccess(candidate.label)

    def persist_provider_update(
        self,
        account: Account,
        *,
        expected_credentials: Credentials,
        expected_plan: str,
    ) -> CredentialUpdateResult:
        """Persist provider-discovered credentials and plan atomically."""
        current = self._store.get(str(account.label))
        if (
            current is None
            or current.provider_id is not account.provider_id
            or current.credentials != expected_credentials
            or current.plan != expected_plan
        ):
            raise SourceChangedError
        candidate = _copy_account(current, credentials=account.credentials)
        candidate.plan = account.plan
        if persist_provider_plan_without_credentials(
            self._store,
            current,
            candidate,
        ):
            return CredentialUpdateSuccess(candidate.label)

        if (
            candidate.provider_id is ProviderId.CODEX
            and candidate.credentials != current.credentials
        ):
            return _failure(
                ProviderId.CODEX,
                ProviderFailureKind.REJECTED,
                "Codex credential changes require official managed login.",
            )
        self._store.persist_credentials(candidate)
        return CredentialUpdateSuccess(candidate.label)

    def refresh(
        self,
        *,
        provider_id: ProviderId,
        label: AccountLabel,
        reason: CredentialRefreshReason,
    ) -> CredentialRefreshResult:
        """Delegate every rotating saved refresh to the coordinator."""
        if self._refresh is None:
            return _failure(
                provider_id,
                ProviderFailureKind.UNSUPPORTED,
                "Credential refresh coordination is unavailable.",
            )
        return self._refresh.refresh(
            provider_id=provider_id,
            label=label,
            reason=reason,
        )

    def login_codex(
        self,
        label: AccountLabel,
        *,
        device_auth: bool,
        events: CodexLoginEventSink,
    ) -> CredentialLoginResult:
        """Authenticate one saved account inside its final managed home."""
        provider = self._providers.get(ProviderId.CODEX)
        if provider is None:
            return _failure(
                ProviderId.CODEX,
                ProviderFailureKind.UNSUPPORTED,
                "Codex provider is not registered.",
            )
        if self._codex_auth_migration is None:
            return _failure(
                ProviderId.CODEX,
                ProviderFailureKind.UNSUPPORTED,
                "Managed Codex login is not available.",
            )
        return self._codex_auth_migration.migrate(
            label,
            device_auth=device_auth,
            events=events,
        )

    def _apply_detected(
        self,
        account: Account,
        detected: DetectedCredentials,
        *,
        replace_identity: bool,
        replace_auth_method: bool,
    ) -> Account | ProviderFailure:
        if account.provider_id is not detected.provider_id:
            return _failure(
                account.provider_id,
                ProviderFailureKind.IDENTITY_MISMATCH,
                "Detected credentials belong to another provider.",
            )
        if isinstance(detected.expiry, InvalidExpiry):
            return _failure(
                account.provider_id,
                ProviderFailureKind.MALFORMED,
                "Detected credential expiry metadata is invalid.",
            )
        current = account.credentials
        incoming = detected.credentials
        if isinstance(
            current,
            ClaudeSetupTokenCredentials | ClaudeLoginCredentials,
        ) and isinstance(
            incoming,
            ClaudeSetupTokenCredentials | ClaudeLoginCredentials,
        ):
            applied = apply_claude_transition(
                current,
                incoming,
                replace_identity=replace_identity,
                replace_auth_method=replace_auth_method,
            )
            if isinstance(applied, ProviderFailure):
                return applied
            account.credentials = applied
        else:
            return _failure(
                account.provider_id,
                ProviderFailureKind.MALFORMED,
                "Detected credentials are provider-incompatible.",
            )
        if detected.plan and detected.plan != "unknown":
            account.plan = detected.plan
        return account

    def _validate_new_account(
        self,
        account: Account,
        provider: Provider,
    ) -> str | ProviderFailure | None:
        outcome: str | ProviderFailure | None = None
        try:
            provider.validate_credentials(account, self._http)
        except AuthError:
            outcome = _failure(
                provider.id,
                ProviderFailureKind.REJECTED,
                "The provider rejected the supplied token.",
            )
        except ForbiddenError as error:
            outcome = self._forbidden_validation(
                account,
                provider,
                error,
            )
        except RateLimitError as error:
            wait = (
                f"retry in {error.retry_after}s"
                if error.retry_after is not None
                else "retry shortly"
            )
            outcome = f"Provider validation was rate-limited; {wait}."
        except TransientError:
            outcome = "Provider validation was temporarily unavailable."
        except ProviderBoundaryError as error:
            outcome = error.failure
        return outcome

    def _forbidden_validation(
        self,
        account: Account,
        provider: Provider,
        error: ForbiddenError,
    ) -> str | None:
        """Return the provider-neutral validation warning for forbidden use."""
        del account, provider, error
        return "Token saved, but the usage endpoint denied this scope."

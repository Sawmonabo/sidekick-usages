"""Provider-neutral credential coordination."""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.expiry import InvalidExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    Credentials,
    DetectedCredentials,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
)
from sidekick_usages.credentials.account_state import (
    persist_provider_plan_without_credentials,
)
from sidekick_usages.credentials.claude.managed.migration.service import (
    ClaudeManagedMigrationCoordinator,
)
from sidekick_usages.credentials.claude.setup.service import (
    ClaudeSetupTokenCoordinator,
)
from sidekick_usages.credentials.codex.migration import (
    CodexAuthMigrationCoordinator,
)
from sidekick_usages.credentials.codex.types import CodexLoginEventSink
from sidekick_usages.credentials.models import (
    CredentialLoginResult,
    CredentialRefreshResult,
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
    "Run `sidekick-usages claude setup-token` to generate one."
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
        refresh_coordinator: CredentialRefreshCoordinator | None = None,
        codex_auth_migration: CodexAuthMigrationCoordinator | None = None,
        claude_auth_migration: ClaudeManagedMigrationCoordinator | None = None,
        claude_setup_tokens: ClaudeSetupTokenCoordinator | None = None,
    ) -> None:
        """Bind credential workflows to invocation-scoped dependencies.

        :param store: Loaded transactional account store.
        :param http: Shared provider HTTP facade.
        :param providers: Closed provider adapter registry.
        """
        self._store = store
        self._http = http
        self._providers = providers
        self._refresh = refresh_coordinator
        self._codex_auth_migration = codex_auth_migration
        self._claude_auth_migration = claude_auth_migration
        self._claude_setup_tokens = claude_setup_tokens

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
        setup_result = self._save_existing_setup_token(
            result,
            label=label,
            plan=plan,
            force=force,
        )
        if setup_result is not None:
            return setup_result
        save_plan = self._build_save_plan(
            source,
            result,
            label=label,
            plan=plan,
            force=force,
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
        target_id = self._store.resolve_account_id(
            source.provider_id,
            target_label,
        )
        saved_target = next(
            (
                account
                for account in self._store.saved_accounts(source.provider_id)
                if account.account_id == target_id
            ),
            None,
        )
        if saved_target is not None and saved_target.has_managed_authority:
            return _failure(
                source.provider_id,
                ProviderFailureKind.REJECTED,
                "Managed account credentials must be repaired with "
                "`sidekick-usages refresh`.",
            )
        target = self._store.get(
            str(target_label),
            provider_id=source.provider_id,
        )
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
            if plan is not None:
                candidate.plan = plan
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

    def _save_existing_setup_token(
        self,
        detected: DetectedCredentials,
        *,
        label: AccountLabel | None,
        plan: str | None,
        force: bool,
    ) -> CredentialSaveResult | None:
        """Attach one setup token to an existing logical Claude account."""
        credentials = detected.credentials
        if label is None or not isinstance(
            credentials, ClaudeSetupTokenCredentials
        ):
            return None
        account_id = self._store.resolve_account_id(
            ProviderId.CLAUDE,
            label,
        )
        if account_id is None:
            return None
        return self._update_existing_setup_token(
            account_id,
            label,
            credentials,
            plan=plan,
            force=force,
        )

    def _update_existing_setup_token(
        self,
        account_id: SidekickAccountId,
        label: AccountLabel,
        credentials: ClaudeSetupTokenCredentials,
        *,
        plan: str | None,
        force: bool,
    ) -> CredentialSaveResult:
        """Persist one explicit existing-account setup-token update."""
        preflight: CredentialSaveResult | None = None
        if not force:
            preflight = _failure(
                ProviderId.CLAUDE,
                ProviderFailureKind.REJECTED,
                f"Account '{label}' already exists; use --force.",
            )
        elif plan is not None:
            preflight = _failure(
                ProviderId.CLAUDE,
                ProviderFailureKind.REJECTED,
                "Update an existing account plan with `set-plan`.",
            )
        if preflight is not None:
            return preflight
        if self._claude_setup_tokens is None:
            return _failure(
                ProviderId.CLAUDE,
                ProviderFailureKind.UNSUPPORTED,
                "Claude setup-token persistence is unavailable.",
            )
        account = self._store.read_saved(account_id)
        if account is None or account.label != label:
            return _failure(
                ProviderId.CLAUDE,
                ProviderFailureKind.MISSING,
                f"No Claude account named '{label}'.",
            )
        provider = self._providers.get(ProviderId.CLAUDE)
        if provider is None:
            return _failure(
                ProviderId.CLAUDE,
                ProviderFailureKind.UNSUPPORTED,
                "Claude provider is not registered.",
            )
        validation = self._validate_new_account(
            Account(
                label=account.label,
                credentials=credentials,
                plan=account.plan,
            ),
            provider,
        )
        if isinstance(validation, ProviderFailure):
            return validation
        saved = self._claude_setup_tokens.save(account, credentials)
        return CredentialSaveSuccess(saved.label, False, validation)

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

    def login_claude(
        self,
        label: AccountLabel,
        *,
        establish_identity: bool,
        interactive: bool,
    ) -> CredentialLoginResult:
        """Authenticate one account inside its final managed Claude profile."""
        if ProviderId.CLAUDE not in self._providers:
            return _failure(
                ProviderId.CLAUDE,
                ProviderFailureKind.UNSUPPORTED,
                "Claude provider is not registered.",
            )
        if self._claude_auth_migration is None:
            return _failure(
                ProviderId.CLAUDE,
                ProviderFailureKind.UNSUPPORTED,
                "Managed Claude login is not available.",
            )
        return self._claude_auth_migration.migrate(
            label,
            establish_identity=establish_identity,
            interactive=interactive,
        )

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

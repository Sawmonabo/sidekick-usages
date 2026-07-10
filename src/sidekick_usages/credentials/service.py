"""Provider-neutral credential coordination."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import InvalidExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
    Credentials,
    DetectedCredentials,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials.codex import CodexCredentialCoordinator
from sidekick_usages.credentials.models import (
    CredentialExportResult,
    CredentialLoginResult,
    CredentialLoginSuccess,
    CredentialRefreshResult,
    CredentialRefreshSuccess,
    CredentialSaveResult,
    CredentialSaveSuccess,
    CredentialSource,
    CredentialUpdateResult,
    CredentialUpdateSuccess,
    LocalCredentialSource,
    TokenCredentialSource,
    TokenPromptSpec,
)
from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    RateLimitError,
    TransientError,
    UsageError,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.errors import SourceChangedError
from sidekick_usages.persistence.private_credentials import (
    PreparedPrivateBundleWrite,
    PrivateCredentialTree,
)
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
)

_CLAUDE_USAGE_REQUIRED_SCOPE = "user:profile"
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
        private_credentials: PrivateCredentialTree,
        *,
        clock: Clock,
    ) -> None:
        """Bind credential workflows to invocation-scoped dependencies.

        :param store: Loaded transactional account store.
        :param http: Shared provider HTTP facade.
        :param providers: Closed provider adapter registry.
        :param private_credentials: Shared private credential tree.
        :param clock: Aware application wall clock.
        """
        self._store = store
        self._http = http
        self._providers = providers
        self._private = private_credentials
        self._clock = clock
        self._codex = CodexCredentialCoordinator(
            store,
            private_credentials,
            clock=clock,
        )

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
            and previous.access_token != candidate.access_token
        ):
            validation = self._validate_new_account(
                candidate,
                self._providers[source.provider_id],
            )
            if isinstance(validation, ProviderFailure):
                return validation
            warning = validation

        private_bundle: PreparedPrivateBundleWrite | None = None
        if candidate.provider_id is ProviderId.CODEX:
            require_bundle = isinstance(source, LocalCredentialSource)
            prepared = self._codex.prepare_account(
                candidate,
                previous,
                source_home=(
                    source.credential_home
                    if isinstance(source, LocalCredentialSource)
                    else None
                ),
                use_existing_source=isinstance(
                    source,
                    TokenCredentialSource,
                ),
                require_bundle=require_bundle,
                reference_time=self._clock.now(),
            )
            if isinstance(prepared, ProviderFailure):
                return prepared
            candidate, private_bundle = prepared

        self._store.persist_credentials(
            candidate,
            previous_label=save_plan.previous_label,
            private_bundle=private_bundle,
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
        if previous is None or replacing:
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
                replace_identity=False,
            )
            if isinstance(applied, ProviderFailure):
                return applied
            candidate = applied
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

    def refresh_from_source(
        self,
        label: str,
        source: LocalCredentialSource,
        *,
        replace_identity: bool,
    ) -> CredentialRefreshResult:
        """Import one local login into an existing saved account."""
        account = self._store.get(label)
        if account is None:
            return _failure(
                source.provider_id,
                ProviderFailureKind.MISSING,
                f"No account named '{label}'.",
            )
        if account.provider_id is not source.provider_id:
            return _failure(
                source.provider_id,
                ProviderFailureKind.IDENTITY_MISMATCH,
                "The credential source belongs to another provider.",
            )
        detected = self.resolve(source)
        if isinstance(detected, ProviderFailure):
            return detected
        candidate = _copy_account(account)
        applied = self._apply_detected(
            candidate,
            detected,
            replace_identity=replace_identity,
        )
        if isinstance(applied, ProviderFailure):
            return applied
        candidate = applied
        reference_time = self._clock.now()
        candidate.last_refresh_at = reference_time
        candidate.last_refresh_status = RefreshStatus.OK
        candidate.last_refresh_error = None
        private_bundle: PreparedPrivateBundleWrite | None = None
        if candidate.provider_id is ProviderId.CODEX:
            prepared = self._codex.prepare_account(
                candidate,
                account,
                source_home=source.credential_home,
                use_existing_source=False,
                require_bundle=True,
                reference_time=reference_time,
            )
            if isinstance(prepared, ProviderFailure):
                return prepared
            candidate, private_bundle = prepared
        self._store.persist_credentials(
            candidate,
            private_bundle=private_bundle,
        )
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
        if (
            candidate.credentials == current.credentials
            and candidate.plan == current.plan
        ):
            return CredentialUpdateSuccess(candidate.label)

        private_bundle: PreparedPrivateBundleWrite | None = None
        if (
            candidate.provider_id is ProviderId.CODEX
            and candidate.credentials != current.credentials
        ):
            prepared = self._codex.prepare_account(
                candidate,
                current,
                source_home=None,
                use_existing_source=True,
                require_bundle=False,
                reference_time=self._clock.now(),
            )
            if isinstance(prepared, ProviderFailure):
                return prepared
            candidate, private_bundle = prepared
        self._store.persist_credentials(
            candidate,
            private_bundle=private_bundle,
        )
        return CredentialUpdateSuccess(candidate.label)

    def refresh_saved(self, account: Account) -> CredentialRefreshResult:
        """Refresh from saved secrets without reading an active login."""
        provider = self._providers.get(account.provider_id)
        if provider is None:
            return _failure(
                account.provider_id,
                ProviderFailureKind.UNSUPPORTED,
                f"Provider '{account.provider_id}' is not registered.",
            )
        try:
            refreshed = provider.refresh_credentials(account, self._http)
        except ProviderBoundaryError as error:
            return error.failure
        if isinstance(refreshed, ProviderFailure):
            return refreshed
        candidate = _copy_account(account, credentials=refreshed.credentials)
        if refreshed.plan is not None:
            candidate.plan = refreshed.plan
        reference_time = self._clock.now()
        candidate.last_refresh_at = reference_time
        candidate.last_refresh_status = RefreshStatus.OK
        candidate.last_refresh_error = None
        private_bundle: PreparedPrivateBundleWrite | None = None
        if candidate.provider_id is ProviderId.CODEX:
            prepared = self._codex.prepare_account(
                candidate,
                account,
                source_home=None,
                use_existing_source=True,
                require_bundle=False,
                reference_time=reference_time,
            )
            if isinstance(prepared, ProviderFailure):
                return prepared
            candidate, private_bundle = prepared
        self._store.persist_credentials(
            candidate,
            private_bundle=private_bundle,
        )
        return CredentialRefreshSuccess(candidate.label)

    def login_codex(
        self,
        label: AccountLabel,
        *,
        source_home: Path | None,
        device_auth: bool,
        replace_identity: bool,
    ) -> CredentialLoginResult:
        """Run Codex login and atomically import its resulting credentials."""
        provider = self._providers.get(ProviderId.CODEX)
        if provider is None:
            return _failure(
                ProviderId.CODEX,
                ProviderFailureKind.UNSUPPORTED,
                "Codex provider is not registered.",
            )
        normalized = (
            source_home.expanduser() if source_home is not None else None
        )
        login = self._codex.login(normalized, device_auth=device_auth)
        if isinstance(login, ProviderFailure):
            return login
        detected = self.resolve(
            LocalCredentialSource(
                provider_id=ProviderId.CODEX,
                credential_home=normalized,
            )
        )
        if isinstance(detected, ProviderFailure):
            return detected
        resolved = self._codex_login_candidate(
            label,
            detected,
            replace_identity=replace_identity,
        )
        if isinstance(resolved, ProviderFailure):
            return resolved
        candidate, existing = resolved
        prepared = self._codex.prepare_account(
            candidate,
            existing,
            source_home=normalized,
            use_existing_source=False,
            require_bundle=True,
            reference_time=self._clock.now(),
        )
        if isinstance(prepared, ProviderFailure):
            return prepared
        candidate, private_bundle = prepared
        self._store.persist_credentials(
            candidate,
            private_bundle=private_bundle,
        )
        return CredentialLoginSuccess(label, existing is None)

    def _codex_login_candidate(
        self,
        label: AccountLabel,
        detected: DetectedCredentials,
        *,
        replace_identity: bool,
    ) -> tuple[Account, Account | None] | ProviderFailure:
        """Build one Codex login candidate without durable mutation."""
        existing = self._store.get(str(label))
        if (
            existing is not None
            and existing.provider_id is not ProviderId.CODEX
        ):
            return _failure(
                ProviderId.CODEX,
                ProviderFailureKind.IDENTITY_MISMATCH,
                f"'{label}' belongs to another provider.",
            )
        if existing is None:
            candidate = Account(
                label=label,
                credentials=detected.credentials,
                plan=detected.plan,
            )
        else:
            candidate = _copy_account(existing)
            applied = self._apply_detected(
                candidate,
                detected,
                replace_identity=replace_identity,
            )
            if isinstance(applied, ProviderFailure):
                return applied
            candidate = applied
        return candidate, existing

    def export_codex(
        self,
        label: str,
        target_home: Path,
        *,
        source_home: Path | None = None,
    ) -> CredentialExportResult:
        """Export one saved account without mutating an active Codex login."""
        account = self._store.get(label)
        if account is None:
            return _failure(
                ProviderId.CODEX,
                ProviderFailureKind.MISSING,
                f"No account named '{label}'.",
            )
        if account.provider_id is not ProviderId.CODEX:
            return _failure(
                ProviderId.CODEX,
                ProviderFailureKind.IDENTITY_MISMATCH,
                f"'{label}' is not a Codex account.",
            )
        result = self._codex.export(
            account,
            target_home,
            source_home=source_home,
        )
        if (
            isinstance(result, ProviderFailure)
            and result.kind is ProviderFailureKind.INCOMPLETE
            and account.refresh_token is not None
        ):
            refreshed = self.refresh_saved(account)
            if isinstance(refreshed, ProviderFailure):
                return refreshed
            saved = self._store.get(label)
            if saved is None:
                raise RuntimeError("Refreshed account disappeared from store.")
            result = self._codex.export(
                saved,
                target_home,
                source_home=source_home,
            )
        return result

    def _apply_detected(
        self,
        account: Account,
        detected: DetectedCredentials,
        *,
        replace_identity: bool,
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
        if isinstance(current, ClaudeCredentials) and isinstance(
            incoming,
            ClaudeCredentials,
        ):
            account.credentials = replace(
                current,
                access_token=incoming.access_token,
                refresh_token=(
                    incoming.refresh_token
                    if incoming.refresh_token is not None
                    else current.refresh_token
                ),
                expiry=(
                    incoming.expiry
                    if not isinstance(incoming.expiry, UnknownExpiry)
                    else current.expiry
                ),
                scopes=(
                    incoming.scopes
                    if incoming.scopes is not None
                    else current.scopes
                ),
            )
        elif isinstance(current, CodexCredentials) and isinstance(
            incoming,
            CodexCredentials,
        ):
            old_id = current.account_id
            new_id = incoming.account_id
            identity_proven = (
                old_id is not None and old_id == new_id
            ) or current.access_token == incoming.access_token
            if not replace_identity and not identity_proven:
                return _failure(
                    ProviderId.CODEX,
                    ProviderFailureKind.IDENTITY_MISMATCH,
                    "Refusing Codex credentials without matching identity; "
                    "use --replace-identity to replace this label.",
                )
            if replace_identity:
                account.credentials = replace(incoming, auth_home=None)
            else:
                account.credentials = replace(
                    current,
                    access_token=incoming.access_token,
                    refresh_token=(
                        incoming.refresh_token
                        if incoming.refresh_token is not None
                        else current.refresh_token
                    ),
                    expiry=(
                        incoming.expiry
                        if not isinstance(incoming.expiry, UnknownExpiry)
                        else current.expiry
                    ),
                    account_id=incoming.account_id or current.account_id,
                    id_token=incoming.id_token or current.id_token,
                    auth_last_refresh=(
                        incoming.auth_last_refresh or current.auth_last_refresh
                    ),
                )
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
            provider.fetch_usage(account, self._http)
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
        """Handle Claude's inference-only usage validation fallback."""
        if not (
            provider.id is ProviderId.CLAUDE
            and error.required_scope == _CLAUDE_USAGE_REQUIRED_SCOPE
            and account.scopes is None
        ):
            return "Token saved, but the usage endpoint denied this scope."
        credentials = account.credentials
        if not isinstance(credentials, ClaudeCredentials):
            raise AssertionError("Claude account has wrong credentials.")
        account.credentials = replace(credentials, scopes=())
        try:
            provider.fetch_usage(account, self._http)
        except UsageError:
            return "Token saved, but the usage validation probe failed."
        return None


__all__ = ["CredentialService"]

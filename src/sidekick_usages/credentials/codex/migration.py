"""Independent official login and stored-to-managed Codex migration."""

from collections.abc import Mapping

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    CodexAccountAuthority,
    CodexManagedAuthority,
    CodexStoredAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.codex.managed.account import (
    managed_codex_account,
)
from sidekick_usages.credentials.codex.managed.failures import (
    codex_app_server_failure,
)
from sidekick_usages.credentials.codex.managed.home import (
    CodexPrivateHomeAuthority,
)
from sidekick_usages.credentials.codex.models import (
    CodexAuthorityExpectation,
)
from sidekick_usages.credentials.codex.types import CodexLoginEventSink
from sidekick_usages.credentials.models import (
    CredentialLoginResult,
    CredentialLoginSuccess,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.authority import (
    CodexLoginLock,
    OperationAuthorityLock,
)
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.account.failures import (
    codex_account_provider_failure,
)
from sidekick_usages.providers.codex.account.service import read_codex_account
from sidekick_usages.providers.codex.account.types import (
    CodexAccountReadFailure,
)
from sidekick_usages.providers.codex.app_server.capabilities import (
    probe_codex_capabilities,
)
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
)
from sidekick_usages.providers.codex.app_server.session import (
    CodexAppServerSession,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.auth.generation import (
    codex_generation_order,
)
from sidekick_usages.providers.codex.auth.login.service import (
    complete_codex_login,
    start_codex_login,
)
from sidekick_usages.providers.codex.auth.models import CodexAuthSnapshot

_RELOGIN_FAILURE_KINDS = frozenset(
    {
        ProviderFailureKind.MISSING,
        ProviderFailureKind.EXPIRED,
        ProviderFailureKind.REJECTED,
    }
)


class CodexAuthMigrationCoordinator:
    """Authenticate final homes and atomically retire legacy authorities."""

    def __init__(
        self,
        paths: ApplicationPaths,
        store: AccountStore,
        managed_private: PrivateCredentialTree,
        clock: Clock,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._paths = paths
        self._store = store
        self._managed_private = managed_private
        self._clock = clock
        self._environment = None if environment is None else dict(environment)

    def migrate(
        self,
        label: AccountLabel,
        *,
        device_auth: bool,
        events: CodexLoginEventSink,
    ) -> CredentialLoginResult:
        """Login or recover one final home without reading native auth."""
        try:
            home = self._capability_proven_home()
        except CodexAppServerError as error:
            return codex_app_server_failure(error)
        account_id = self._store.resolve_account_id(ProviderId.CODEX, label)
        if account_id is None:
            return _missing_label(label)
        login_lock = CodexLoginLock(self._paths.durable_operations)
        authority_lock = OperationAuthorityLock(
            self._paths.durable_operations,
            account_id,
        )
        try:
            with login_lock.hold(), authority_lock.hold():
                return self._migrate_locked(
                    account_id,
                    label,
                    home,
                    device_auth=device_auth,
                    events=events,
                )
        except CodexAppServerError as error:
            return codex_app_server_failure(error)
        except PersistenceError:
            return _failure(
                ProviderFailureKind.UNREADABLE,
                "Managed Codex login coordination is unavailable; "
                "retry shortly.",
                action_required=False,
            )

    def _capability_proven_home(self) -> CodexPrivateHomeAuthority:
        executable = discover_codex_executable(self._environment)
        capabilities = probe_codex_capabilities(
            executable,
            self._environment,
        )
        return CodexPrivateHomeAuthority(
            self._paths,
            self._managed_private,
            capabilities,
            environment=self._environment,
        )

    def _migrate_locked(
        self,
        account_id: SidekickAccountId,
        label: AccountLabel,
        home: CodexPrivateHomeAuthority,
        *,
        device_auth: bool,
        events: CodexLoginEventSink,
    ) -> CredentialLoginResult:
        account = self._current_account(account_id, label)
        if isinstance(account, ProviderFailure):
            return account
        expected = _expected_authority(account)
        if isinstance(expected, ProviderFailure):
            return expected
        configured = home.configure(account_id)
        if configured is not None:
            return configured
        snapshot = self._authenticate_and_refresh(
            account,
            expected,
            home,
            device_auth=device_auth,
            events=events,
        )
        if isinstance(snapshot, ProviderFailure):
            return snapshot
        candidate = managed_codex_account(
            account,
            expected.authority_id,
            snapshot,
            plan=account.plan,
            executable_version=home.executable_version,
            verified_at=self._clock.now(),
            refreshed=True,
        )
        commit_failure = self._commit(account, candidate)
        if commit_failure is not None:
            return commit_failure
        return CredentialLoginSuccess(label)

    def _current_account(
        self,
        account_id: SidekickAccountId,
        label: AccountLabel,
    ) -> SavedAccount | ProviderFailure:
        account = self._store.read_saved(account_id)
        current_id = self._store.resolve_account_id(ProviderId.CODEX, label)
        if (
            account is None
            or current_id != account_id
            or account.label != label
        ):
            return _missing_label(label)
        return account

    def _authenticate_and_refresh(
        self,
        account: SavedAccount,
        expected: CodexAuthorityExpectation,
        home: CodexPrivateHomeAuthority,
        *,
        device_auth: bool,
        events: CodexLoginEventSink,
    ) -> CodexAuthSnapshot | ProviderFailure:
        existing = home.snapshot(account.account_id)
        login_required = _login_required(
            existing,
            expected.provider_identity,
        )
        if isinstance(login_required, ProviderFailure):
            return login_required
        if login_required:
            return self._login_and_refresh(
                account,
                expected,
                home,
                device_auth=device_auth,
                events=events,
            )
        recovered = self._recover_final_home(account, expected, home)
        if (
            not isinstance(recovered, ProviderFailure)
            or recovered.kind not in _RELOGIN_FAILURE_KINDS
        ):
            return recovered
        return self._login_and_refresh(
            account,
            expected,
            home,
            device_auth=device_auth,
            events=events,
        )

    def _recover_final_home(
        self,
        account: SavedAccount,
        expected: CodexAuthorityExpectation,
        home: CodexPrivateHomeAuthority,
    ) -> CodexAuthSnapshot | ProviderFailure:
        """Try one same-identity refresh before requiring another login."""
        with home.open_session(account.account_id) as session:
            observed = read_codex_account(
                session,
                refresh_token=False,
            )
            if isinstance(observed, CodexAccountReadFailure):
                return codex_account_provider_failure(observed)
            try:
                return self._force_refresh(
                    session,
                    home,
                    account.account_id,
                    expected,
                )
            except CodexAppServerError as error:
                if error.code is not CodexAppServerFailure.REQUEST_REJECTED:
                    raise
                return _failure(
                    ProviderFailureKind.REJECTED,
                    "Codex rejected the existing managed login.",
                )

    def _login_and_refresh(
        self,
        account: SavedAccount,
        expected: CodexAuthorityExpectation,
        home: CodexPrivateHomeAuthority,
        *,
        device_auth: bool,
        events: CodexLoginEventSink,
    ) -> CodexAuthSnapshot | ProviderFailure:
        """Perform one official login followed by one forced refresh."""
        with home.open_session(account.account_id) as session:
            attempt = start_codex_login(
                session,
                device_auth=device_auth,
            )
            events(attempt.event)
            completed = complete_codex_login(session, attempt)
            if isinstance(completed, ProviderFailure):
                return completed
            return self._force_refresh(
                session,
                home,
                account.account_id,
                expected,
            )

    @staticmethod
    def _force_refresh(
        session: CodexAppServerSession,
        home: CodexPrivateHomeAuthority,
        account_id: SidekickAccountId,
        expected: CodexAuthorityExpectation,
    ) -> CodexAuthSnapshot | ProviderFailure:
        before = _verified_snapshot(
            home,
            account_id,
            expected.provider_identity,
        )
        if isinstance(before, ProviderFailure):
            return before
        refreshed = read_codex_account(session, refresh_token=True)
        if isinstance(refreshed, CodexAccountReadFailure):
            return codex_account_provider_failure(refreshed)
        after = _verified_snapshot(
            home,
            account_id,
            expected.provider_identity,
        )
        if isinstance(after, ProviderFailure):
            return after
        if not after.advanced_from(before):
            return _failure(
                ProviderFailureKind.REJECTED,
                "Codex did not advance the managed credential generation.",
            )
        if expected.baseline is not None and not after.advanced_from(
            expected.baseline
        ):
            return _failure(
                ProviderFailureKind.REJECTED,
                "Codex did not advance beyond the saved credential "
                "generation.",
            )
        return after

    def _commit(
        self,
        current: SavedAccount,
        candidate: SavedAccount,
    ) -> ProviderFailure | None:
        try:
            if isinstance(
                current.authority.subscription,
                CodexStoredAuthority,
            ):
                self._store.migrate_stored_authority(
                    candidate,
                    expected=current,
                )
            else:
                self._store.persist_state(candidate, expected=current)
        except OSError, PersistenceError:
            return _failure(
                ProviderFailureKind.UNREADABLE,
                "The managed Codex login could not be committed safely.",
                action_required=False,
            )
        return None


def _login_required(
    existing: CodexAuthSnapshot | ProviderFailure,
    expected_identity: ProviderIdentity,
) -> bool | ProviderFailure:
    if isinstance(existing, ProviderFailure):
        if existing.kind is ProviderFailureKind.MISSING:
            return True
        return existing
    if existing.provider_identity != expected_identity:
        return _identity_failure()
    return False


def _verified_snapshot(
    home: CodexPrivateHomeAuthority,
    account_id: SidekickAccountId,
    expected_identity: ProviderIdentity,
) -> CodexAuthSnapshot | ProviderFailure:
    snapshot = home.snapshot(account_id)
    if isinstance(snapshot, ProviderFailure):
        return snapshot
    if snapshot.provider_identity != expected_identity:
        return _identity_failure()
    return snapshot


def _expected_authority(
    account: SavedAccount,
) -> CodexAuthorityExpectation | ProviderFailure:
    authority = account.authority
    if account.provider_id is not ProviderId.CODEX or not isinstance(
        authority, CodexAccountAuthority
    ):
        return _failure(
            ProviderFailureKind.IDENTITY_MISMATCH,
            "The saved label does not belong to Codex.",
        )
    subscription = authority.subscription
    if isinstance(subscription, CodexManagedAuthority):
        identity = subscription.provider_identity
        generation = subscription.generation
    elif isinstance(subscription, CodexStoredAuthority):
        identity = subscription.provider_identity
        generation = subscription.generation
        if identity is None:
            return _failure(
                ProviderFailureKind.INCOMPLETE,
                "The saved Codex account has no verified provider identity.",
            )
    else:
        raise AssertionError("Codex account has an invalid authority.")
    baseline: CodexAuthSnapshot | None = None
    if generation is not None:
        try:
            order = codex_generation_order(str(generation))
        except ValueError:
            return _failure(
                ProviderFailureKind.MALFORMED,
                "The managed Codex credential generation is malformed.",
            )
        baseline = CodexAuthSnapshot(
            provider_identity=identity,
            generation=generation,
            generation_order=order,
            plan=account.plan,
        )
    return CodexAuthorityExpectation(
        authority_id=subscription.authority_id,
        provider_identity=identity,
        baseline=baseline,
    )


def _missing_label(label: AccountLabel) -> ProviderFailure:
    return _failure(
        ProviderFailureKind.MISSING,
        f"No saved Codex account named '{label}'.",
    )


def _identity_failure() -> ProviderFailure:
    return _failure(
        ProviderFailureKind.IDENTITY_MISMATCH,
        "The final Codex home belongs to a different saved account.",
    )


def _failure(
    kind: ProviderFailureKind,
    message: str,
    *,
    action_required: bool = True,
) -> ProviderFailure:
    return ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=kind,
        message=message,
        action_required=action_required,
    )

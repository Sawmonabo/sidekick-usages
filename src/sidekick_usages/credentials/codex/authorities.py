"""Managed private-home coordination for Codex authorities."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityId,
    CredentialHealth,
    SidekickAccountId,
)
from sidekick_usages.core.models import CodexCredentials
from sidekick_usages.core.types import ProviderId, RefreshStatus
from sidekick_usages.credentials.codex.models import (
    CodexManagedAuthorityResult,
    CodexProjectionLease,
)
from sidekick_usages.credentials.codex.ports import CodexProjectionInstaller
from sidekick_usages.credentials.codex.types import CodexManagedOutcome
from sidekick_usages.paths import ApplicationPaths, managed_codex_home
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import PersistenceFilesystemError
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
    OperationAuthorityLock,
)
from sidekick_usages.persistence.types.error import PersistenceCode
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.account import read_codex_account
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.models import (
    CodexAppServerCapabilities,
)
from sidekick_usages.providers.codex.app_server.session import (
    CodexAppServerSession,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.auth import (
    CODEX_AUTH_FILE,
    CODEX_CONFIG_FILE,
    codex_generation_order,
    managed_auth_snapshot,
    parse_managed_auth_credentials,
    parse_managed_auth_snapshot,
    prepare_file_auth_config,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexProjectionReceipt,
)
from sidekick_usages.providers.codex.models import (
    CodexAccountObservation,
    CodexAuthSnapshot,
)

_APP_SERVER_OUTCOMES = {
    CodexAppServerFailure.EXECUTABLE_MISSING: CodexManagedOutcome.INCOMPATIBLE,
    CodexAppServerFailure.EXECUTABLE_UNSAFE: CodexManagedOutcome.INCOMPATIBLE,
    CodexAppServerFailure.VERSION_UNSUPPORTED: (
        CodexManagedOutcome.INCOMPATIBLE
    ),
    CodexAppServerFailure.CAPABILITY_UNSUPPORTED: (
        CodexManagedOutcome.INCOMPATIBLE
    ),
    CodexAppServerFailure.PROCESS_FAILED: CodexManagedOutcome.TRANSIENT,
    CodexAppServerFailure.PROCESS_TIMEOUT: CodexManagedOutcome.TIMED_OUT,
    CodexAppServerFailure.PROTOCOL_MALFORMED: CodexManagedOutcome.MALFORMED,
    CodexAppServerFailure.REQUEST_REJECTED: CodexManagedOutcome.REJECTED,
    CodexAppServerFailure.PROTOCOL_TIMEOUT: CodexManagedOutcome.TIMED_OUT,
    CodexAppServerFailure.PROTOCOL_CLOSED: CodexManagedOutcome.TRANSIENT,
}
_APP_SERVER_FAILURE_KINDS = {
    CodexManagedOutcome.INCOMPATIBLE: ProviderFailureKind.UNSUPPORTED,
    CodexManagedOutcome.TRANSIENT: ProviderFailureKind.UNREADABLE,
    CodexManagedOutcome.TIMED_OUT: ProviderFailureKind.UNREADABLE,
    CodexManagedOutcome.MALFORMED: ProviderFailureKind.MALFORMED,
    CodexManagedOutcome.REJECTED: ProviderFailureKind.REJECTED,
}
_PROVIDER_OUTCOMES = {
    ProviderFailureKind.MISSING: CodexManagedOutcome.LOGGED_OUT,
    ProviderFailureKind.UNREADABLE: CodexManagedOutcome.TRANSIENT,
    ProviderFailureKind.MALFORMED: CodexManagedOutcome.MALFORMED,
    ProviderFailureKind.INCOMPLETE: CodexManagedOutcome.MALFORMED,
    ProviderFailureKind.EXPIRED: CodexManagedOutcome.REJECTED,
    ProviderFailureKind.REJECTED: CodexManagedOutcome.REJECTED,
    ProviderFailureKind.IDENTITY_MISMATCH: CodexManagedOutcome.REJECTED,
    ProviderFailureKind.UNSUPPORTED: CodexManagedOutcome.INCOMPATIBLE,
}
_OUTCOME_HEALTH = {
    CodexManagedOutcome.HEALTHY: CredentialHealth.HEALTHY,
    CodexManagedOutcome.UNCHANGED: CredentialHealth.REFRESH_DUE,
    CodexManagedOutcome.REJECTED: CredentialHealth.LOGIN_REQUIRED,
    CodexManagedOutcome.LOGGED_OUT: CredentialHealth.LOGIN_REQUIRED,
    CodexManagedOutcome.INCOMPATIBLE: CredentialHealth.UNSUPPORTED,
    CodexManagedOutcome.MALFORMED: CredentialHealth.MALFORMED,
    CodexManagedOutcome.TIMED_OUT: CredentialHealth.REFRESH_DUE,
    CodexManagedOutcome.TRANSIENT: CredentialHealth.REFRESH_DUE,
}
_PERSISTENCE_FAILURE_KINDS = {
    PersistenceCode.UNSUPPORTED_FILESYSTEM: ProviderFailureKind.UNSUPPORTED,
    PersistenceCode.UNSAFE_PERMISSIONS: ProviderFailureKind.MALFORMED,
    PersistenceCode.UNREADABLE: ProviderFailureKind.UNREADABLE,
    PersistenceCode.INVALID_SCHEMA: ProviderFailureKind.MALFORMED,
}


class CodexPrivateHomeAuthority:
    """Qualified private-home access shared by Codex credential workflows."""

    def __init__(
        self,
        paths: ApplicationPaths,
        private: PrivateCredentialTree,
        capabilities: CodexAppServerCapabilities,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if private.root != paths.private_codex_profiles:
            raise ValueError("Managed Codex tree does not match app paths.")
        self._paths = paths
        self._private = private
        self._capabilities = capabilities
        self._environment = None if environment is None else dict(environment)

    @property
    def executable_version(self) -> str:
        """Return the exact capability-proven Codex version."""
        return str(self._capabilities.executable.version)

    def configure(
        self,
        account_id: SidekickAccountId,
    ) -> ProviderFailure | None:
        """Create or verify one final home with file-backed auth storage."""
        relative = str(account_id)
        home = managed_codex_home(self._paths, account_id)
        try:
            present = self._private.relative_bundle_present(relative)
            current = self._private.read_relative_bundle_file(
                relative,
                CODEX_CONFIG_FILE,
            )
            prepared = prepare_file_auth_config(
                None if current is None else current.data
            )
            if isinstance(prepared, ProviderFailure):
                return prepared
            if current is not None and current.data == prepared:
                return None
            self._private.write_bundle(
                home,
                {CODEX_CONFIG_FILE: prepared},
                expected_bundle_present=present,
                expected_files={
                    CODEX_CONFIG_FILE: (
                        None if current is None else current.data
                    )
                },
            )
        except PersistenceFilesystemError as error:
            return _private_failure(
                error,
                "The managed Codex home cannot be prepared safely.",
            )
        return None

    def snapshot(
        self,
        account_id: SidekickAccountId,
    ) -> CodexAuthSnapshot | ProviderFailure:
        """Read protected identity and generation through qualified paths."""
        files = self._read_authority(account_id)
        if isinstance(files, ProviderFailure):
            return files
        auth_payload, config_payload = files
        return parse_managed_auth_snapshot(auth_payload, config_payload)

    def projection(
        self,
        account_id: SidekickAccountId,
        expected: CodexAuthSnapshot,
    ) -> CodexProjectionLease | ProviderFailure:
        """Open one locally identity-bound access-token projection."""
        files = self._read_authority(account_id)
        if isinstance(files, ProviderFailure):
            return files
        auth_payload, config_payload = files
        detected = parse_managed_auth_credentials(
            auth_payload,
            config_payload,
        )
        if isinstance(detected, ProviderFailure):
            return detected
        snapshot = managed_auth_snapshot(detected)
        if isinstance(snapshot, ProviderFailure):
            return snapshot
        credentials = detected.credentials
        if (
            not isinstance(credentials, CodexCredentials)
            or credentials.account_id is None
            or snapshot.provider_identity != expected.provider_identity
            or snapshot.generation != expected.generation
            or credentials.account_id != str(expected.provider_identity)
        ):
            return ProviderFailure(
                provider_id=ProviderId.CODEX,
                kind=ProviderFailureKind.IDENTITY_MISMATCH,
                message=(
                    "The managed Codex projection identity is inconsistent."
                ),
            )
        return CodexProjectionLease(
            account_id,
            snapshot.provider_identity,
            snapshot.generation,
            snapshot.plan,
            credentials.access_token,
        )

    def _read_authority(
        self,
        account_id: SidekickAccountId,
    ) -> tuple[bytes | None, bytes | None] | ProviderFailure:
        relative = str(account_id)
        try:
            auth = self._private.read_relative_bundle_file(
                relative,
                CODEX_AUTH_FILE,
            )
            config = self._private.read_relative_bundle_file(
                relative,
                CODEX_CONFIG_FILE,
            )
        except PersistenceFilesystemError as error:
            return _private_failure(
                error,
                "The managed Codex home cannot be read safely.",
            )
        return (
            None if auth is None else auth.data,
            None if config is None else config.data,
        )

    def open_session(
        self,
        account_id: SidekickAccountId,
    ) -> CodexAppServerSession:
        """Open one bounded app server against the exact final home."""
        return CodexAppServerSession.open(
            self._capabilities,
            managed_codex_home(self._paths, account_id),
            self._environment,
        )


class CodexManagedAuthorityCoordinator:
    """Read and refresh one stable provider-owned Codex home at a time."""

    def __init__(
        self,
        paths: ApplicationPaths,
        store: AccountStore,
        private: PrivateCredentialTree,
        capabilities: CodexAppServerCapabilities,
        clock: Clock,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._paths = paths
        self._store = store
        self._capabilities = capabilities
        self._clock = clock
        self._home = CodexPrivateHomeAuthority(
            paths,
            private,
            capabilities,
            environment=environment,
        )

    def read(
        self,
        account_id: SidekickAccountId,
    ) -> CodexManagedAuthorityResult:
        """Read one private account without asking Codex to refresh it."""
        lock = OperationAuthorityLock(
            self._paths.durable_operations,
            account_id,
        )
        with lock.hold() as authority:
            return self.read_with_authority(account_id, authority)

    def refresh(
        self,
        account_id: SidekickAccountId,
    ) -> CodexManagedAuthorityResult:
        """Force one private account through official managed refresh."""
        lock = OperationAuthorityLock(
            self._paths.durable_operations,
            account_id,
        )
        with lock.hold() as authority:
            return self.refresh_with_authority(account_id, authority)

    def read_with_authority(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
    ) -> CodexManagedAuthorityResult:
        """Read while an isolated worker owns this account authority."""
        return self._operate_with_authority(
            account_id,
            authority,
            refresh_token=False,
        )

    def refresh_with_authority(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
    ) -> CodexManagedAuthorityResult:
        """Refresh while an isolated worker owns this account authority."""
        return self._operate_with_authority(
            account_id,
            authority,
            refresh_token=True,
        )

    def install_current_projection(
        self,
        account_id: SidekickAccountId,
        installer: CodexProjectionInstaller,
    ) -> CodexProjectionReceipt | CodexManagedAuthorityResult:
        """Install the current proven generation without forcing refresh."""
        lock = OperationAuthorityLock(
            self._paths.durable_operations,
            account_id,
        )
        with lock.hold() as authority:
            return self.install_current_projection_with_authority(
                account_id,
                authority,
                installer,
            )

    def install_current_projection_with_authority(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
        installer: CodexProjectionInstaller,
    ) -> CodexProjectionReceipt | CodexManagedAuthorityResult:
        """Install current auth while one worker owns this account."""
        authority.require(account_id)
        account = self._saved_account(account_id)
        expected = self._expected_snapshot(account)
        if isinstance(expected, ProviderFailure):
            return self._persist_provider_failure(account, expected)
        return self._install_projection(
            account,
            expected,
            installer,
        )

    def refresh_and_install_projection(
        self,
        account_id: SidekickAccountId,
        installer: CodexProjectionInstaller,
    ) -> CodexProjectionReceipt | CodexManagedAuthorityResult:
        """Force official refresh before installing a fresh projection."""
        lock = OperationAuthorityLock(
            self._paths.durable_operations,
            account_id,
        )
        with lock.hold() as authority:
            return self.refresh_and_install_projection_with_authority(
                account_id,
                authority,
                installer,
            )

    def refresh_and_install_projection_with_authority(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
        installer: CodexProjectionInstaller,
    ) -> CodexProjectionReceipt | CodexManagedAuthorityResult:
        """Refresh and project while one worker owns this account."""
        refreshed = self.refresh_with_authority(account_id, authority)
        if refreshed.outcome is not CodexManagedOutcome.HEALTHY:
            return refreshed
        expected = self._expected_snapshot(refreshed.account)
        if isinstance(expected, ProviderFailure):
            return self._persist_provider_failure(
                refreshed.account,
                expected,
            )
        return self._install_projection(
            refreshed.account,
            expected,
            installer,
        )

    def _operate_with_authority(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
        *,
        refresh_token: bool,
    ) -> CodexManagedAuthorityResult:
        authority.require(account_id)
        account = self._saved_account(account_id)
        expected = self._expected_snapshot(account)
        if isinstance(expected, ProviderFailure):
            return self._persist_provider_failure(account, expected)
        before = self._snapshot(account_id)
        if isinstance(before, ProviderFailure):
            return self._persist_provider_failure(account, before)
        if (
            before.provider_identity != expected.provider_identity
            or not before.not_older_than(expected)
        ):
            return self._persist_failure(
                account,
                CodexManagedOutcome.REJECTED,
                health=CredentialHealth.RECONCILIATION_REQUIRED,
            )
        return self._run_app_server(
            account,
            before,
            refresh_token=refresh_token,
        )

    def _install_projection(
        self,
        account: SavedAccount,
        expected: CodexAuthSnapshot,
        installer: CodexProjectionInstaller,
    ) -> CodexProjectionReceipt | CodexManagedAuthorityResult:
        ready = installer.prepare(
            account.account_id,
            expected.provider_identity,
            expected.generation,
        )
        if ready is not None:
            current = self._snapshot(account.account_id)
            if isinstance(current, ProviderFailure):
                return self._persist_provider_failure(account, current)
            if (
                current.provider_identity != expected.provider_identity
                or current.generation != expected.generation
            ):
                return self._persist_failure(
                    account,
                    CodexManagedOutcome.REJECTED,
                    health=CredentialHealth.RECONCILIATION_REQUIRED,
                )
            return ready
        projection = self._home.projection(account.account_id, expected)
        if isinstance(projection, ProviderFailure):
            return self._persist_provider_failure(account, projection)
        with projection:
            return installer.install(projection)

    def _run_app_server(
        self,
        account: SavedAccount,
        before: CodexAuthSnapshot,
        *,
        refresh_token: bool,
    ) -> CodexManagedAuthorityResult:
        try:
            session = self._home.open_session(account.account_id)
        except CodexAppServerError as error:
            return self._persist_failure(
                account,
                _APP_SERVER_OUTCOMES[error.code],
            )
        observed: CodexAccountObservation | ProviderFailure | None = None
        app_error: CodexAppServerError | None = None
        try:
            with session:
                observed = read_codex_account(
                    session,
                    refresh_token=refresh_token,
                )
        except CodexAppServerError as error:
            app_error = error
        after = self._snapshot(account.account_id)
        if isinstance(after, ProviderFailure):
            return self._persist_provider_failure(account, after)
        if app_error is not None:
            return self._persist_failure(
                account,
                _APP_SERVER_OUTCOMES[app_error.code],
            )
        if isinstance(observed, ProviderFailure):
            return self._persist_provider_failure(account, observed)
        if observed is None:
            return self._persist_failure(
                account,
                CodexManagedOutcome.TRANSIENT,
            )
        return self._complete_exchange(
            account,
            before,
            after,
            observed,
            refresh_token=refresh_token,
        )

    def _complete_exchange(
        self,
        account: SavedAccount,
        before: CodexAuthSnapshot,
        after: CodexAuthSnapshot,
        observed: CodexAccountObservation,
        *,
        refresh_token: bool,
    ) -> CodexManagedAuthorityResult:
        if (
            after.provider_identity != before.provider_identity
            or after.provider_identity
            != self._managed_authority(account).provider_identity
        ):
            return self._persist_failure(
                account,
                CodexManagedOutcome.REJECTED,
                health=CredentialHealth.RECONCILIATION_REQUIRED,
            )
        if refresh_token and not after.advanced_from(before):
            return self._persist_failure(
                account,
                CodexManagedOutcome.UNCHANGED,
            )
        if not refresh_token and not after.not_older_than(before):
            return self._persist_failure(
                account,
                CodexManagedOutcome.REJECTED,
                health=CredentialHealth.RECONCILIATION_REQUIRED,
            )
        return self._persist_success(
            account,
            after,
            observed,
            refreshed=refresh_token,
        )

    def _snapshot(
        self,
        account_id: SidekickAccountId,
    ) -> CodexAuthSnapshot | ProviderFailure:
        return self._home.snapshot(account_id)

    def _expected_snapshot(
        self,
        account: SavedAccount,
    ) -> CodexAuthSnapshot | ProviderFailure:
        authority = self._managed_authority(account)
        order = codex_generation_order(str(authority.generation))
        if isinstance(order, ProviderFailure):
            return order
        return CodexAuthSnapshot(
            provider_identity=authority.provider_identity,
            generation=authority.generation,
            generation_order=order,
            plan=account.plan,
        )

    def _saved_account(self, account_id: SidekickAccountId) -> SavedAccount:
        account = self._store.read_saved(account_id)
        if account is None:
            raise ValueError("Managed Codex account does not exist.")
        self._managed_authority(account)
        return account

    @staticmethod
    def _managed_authority(account: SavedAccount) -> CodexManagedAuthority:
        authority = account.authority
        if not isinstance(authority, CodexAccountAuthority):
            raise ValueError("Account is not managed by Codex.")
        subscription = authority.subscription
        if not isinstance(subscription, CodexManagedAuthority):
            raise ValueError("Codex account is not a managed authority.")
        return subscription

    def _persist_provider_failure(
        self,
        account: SavedAccount,
        failure: ProviderFailure,
    ) -> CodexManagedAuthorityResult:
        return self._persist_failure(
            account,
            _PROVIDER_OUTCOMES[failure.kind],
        )

    def _persist_failure(
        self,
        account: SavedAccount,
        outcome: CodexManagedOutcome,
        *,
        health: CredentialHealth | None = None,
    ) -> CodexManagedAuthorityResult:
        candidate = replace(
            account,
            credential_health=(
                _OUTCOME_HEALTH[outcome] if health is None else health
            ),
            last_refresh_at=self._clock.now(),
            last_refresh_status=RefreshStatus.FAILED,
            last_refresh_error_code=f"codex_managed_{outcome.value}",
        )
        self._store.persist_state(candidate, expected=account)
        return CodexManagedAuthorityResult(outcome, candidate)

    def _persist_success(
        self,
        account: SavedAccount,
        snapshot: CodexAuthSnapshot,
        observed: CodexAccountObservation,
        *,
        refreshed: bool,
    ) -> CodexManagedAuthorityResult:
        previous = self._managed_authority(account)
        verified_at = self._clock.now()
        candidate = managed_codex_account(
            account,
            previous.authority_id,
            snapshot,
            plan=observed.plan,
            executable_version=str(self._capabilities.executable.version),
            verified_at=verified_at,
            refreshed=refreshed,
        )
        self._store.persist_state(candidate, expected=account)
        return CodexManagedAuthorityResult(
            CodexManagedOutcome.HEALTHY,
            candidate,
        )


def codex_app_server_failure(
    error: CodexAppServerError,
) -> ProviderFailure:
    """Convert one secret-safe app-server error to provider vocabulary."""
    outcome = _APP_SERVER_OUTCOMES[error.code]
    return ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=_APP_SERVER_FAILURE_KINDS[outcome],
        message=str(error),
        action_required=outcome
        not in {
            CodexManagedOutcome.TIMED_OUT,
            CodexManagedOutcome.TRANSIENT,
        },
    )


def managed_codex_account(
    account: SavedAccount,
    authority_id: AuthorityId,
    snapshot: CodexAuthSnapshot,
    *,
    plan: str,
    executable_version: str,
    verified_at: datetime,
    refreshed: bool,
) -> SavedAccount:
    """Build one healthy managed account from a proven private snapshot."""
    authority = CodexManagedAuthority(
        authority_id=authority_id,
        provider_identity=snapshot.provider_identity,
        generation=snapshot.generation,
        verified_at=verified_at,
        executable_version=executable_version,
        health=CredentialHealth.HEALTHY,
    )
    return replace(
        account,
        plan=plan,
        authority=CodexAccountAuthority(subscription=authority),
        credential_health=CredentialHealth.HEALTHY,
        last_refresh_at=(
            verified_at if refreshed else account.last_refresh_at
        ),
        last_refresh_status=(
            RefreshStatus.OK if refreshed else account.last_refresh_status
        ),
        last_refresh_error_code=(
            None if refreshed else account.last_refresh_error_code
        ),
    )


def _private_failure(
    error: PersistenceFilesystemError,
    message: str,
) -> ProviderFailure:
    kind = _PERSISTENCE_FAILURE_KINDS.get(
        error.code,
        ProviderFailureKind.MALFORMED,
    )
    return ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=kind,
        message=message,
    )

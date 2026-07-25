"""Managed Codex authority coordination."""

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
from sidekick_usages.core.types import ProviderId, RefreshStatus
from sidekick_usages.credentials.codex.managed.home import (
    CodexPrivateHomeAuthority,
)
from sidekick_usages.credentials.codex.models import (
    CodexManagedAuthorityResult,
    CodexProjectionLease,
    CodexVerifiedAuthorityExchange,
    require_managed_codex_authority,
)
from sidekick_usages.credentials.codex.ports import CodexProjectionInstaller
from sidekick_usages.credentials.codex.types import CodexManagedOutcome
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
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
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.models import (
    CodexAppServerCapabilities,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
    CodexProcessGroupPolicy,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexProjectionExpectation,
    CodexProjectionReceipt,
)
from sidekick_usages.providers.codex.generation import codex_generation_order
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
        exchange = self._prepare_with_authority(
            account_id,
            authority,
            refresh_token=False,
        )
        return (
            exchange
            if isinstance(exchange, CodexManagedAuthorityResult)
            else self._persist_exchange(exchange)
        )

    def refresh_with_authority(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
        *,
        process_group: CodexProcessGroupPolicy = (
            CodexProcessGroupPolicy.ISOLATED
        ),
    ) -> CodexManagedAuthorityResult:
        """Refresh while an isolated worker owns this account authority."""
        exchange = self._prepare_with_authority(
            account_id,
            authority,
            refresh_token=True,
            process_group=process_group,
        )
        return (
            exchange
            if isinstance(exchange, CodexManagedAuthorityResult)
            else self._persist_exchange(exchange)
        )

    def projection_expectation_with_authority(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
    ) -> CodexProjectionExpectation | CodexManagedAuthorityResult:
        """Validate one protected private authority without refreshing it."""
        authority.require(account_id)
        account = self._saved_account(account_id)
        expected = self._expected_snapshot(account)
        if isinstance(expected, ProviderFailure):
            return self._persist_provider_failure(account, expected)
        current = self._snapshot(account_id)
        if isinstance(current, ProviderFailure):
            return self._persist_provider_failure(account, current)
        if (
            current.provider_identity != expected.provider_identity
            or not current.not_older_than(expected)
        ):
            return self._persist_failure(
                account,
                CodexManagedOutcome.REJECTED,
                health=CredentialHealth.RECONCILIATION_REQUIRED,
            )
        return CodexProjectionExpectation(
            account_id,
            current.provider_identity,
            current.generation,
        )

    def stage_refresh_with_authority(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
        expected: CodexProjectionExpectation,
    ) -> CodexVerifiedAuthorityExchange | CodexManagedAuthorityResult:
        """Stage a proven callback refresh without committing success."""
        return self._prepare_expected_projection(
            account_id,
            authority,
            expected,
            refresh_token=True,
        )

    def open_staged_projection_with_authority(
        self,
        exchange: CodexVerifiedAuthorityExchange,
        authority: OperationAuthority,
    ) -> CodexProjectionLease | CodexManagedAuthorityResult:
        """Open the exact staged projection while authority remains held."""
        account_id = exchange.source.account_id
        authority.require(account_id)
        if self._saved_account(account_id) != exchange.source:
            raise ValueError("Managed Codex account changed during refresh.")
        current = self._snapshot(account_id)
        if isinstance(current, ProviderFailure):
            return self._persist_provider_failure(
                exchange.source,
                current,
            )
        if current != exchange.after:
            return self._persist_failure(
                exchange.source,
                CodexManagedOutcome.REJECTED,
                health=CredentialHealth.RECONCILIATION_REQUIRED,
            )
        projection = self._home.projection(account_id, exchange.after)
        if isinstance(projection, ProviderFailure):
            return self._persist_provider_failure(
                exchange.source,
                projection,
            )
        return projection

    def commit_staged_authority_with_authority(
        self,
        exchange: CodexVerifiedAuthorityExchange,
        authority: OperationAuthority,
    ) -> CodexManagedAuthorityResult:
        """Commit one proven private generation under account authority."""
        authority.require(exchange.source.account_id)
        current = self._snapshot(exchange.source.account_id)
        if isinstance(current, ProviderFailure):
            return self._persist_provider_failure(
                exchange.source,
                current,
            )
        if current != exchange.after:
            return self._persist_failure(
                exchange.source,
                CodexManagedOutcome.REJECTED,
                health=CredentialHealth.RECONCILIATION_REQUIRED,
            )
        if exchange.after == exchange.before:
            return CodexManagedAuthorityResult(
                CodexManagedOutcome.HEALTHY,
                exchange.source,
            )
        return self._persist_exchange(exchange)

    def stage_rehydration_with_authority(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
        expected: CodexProjectionExpectation,
    ) -> CodexVerifiedAuthorityExchange | CodexManagedAuthorityResult:
        """Stage current provider state, including crash-forward recovery."""
        authority.require(account_id)
        account = self._saved_account(account_id)
        saved = self._expected_snapshot(account)
        if isinstance(saved, ProviderFailure):
            return self._persist_provider_failure(account, saved)
        try:
            selected = CodexAuthSnapshot(
                provider_identity=expected.provider_identity,
                generation=expected.generation,
                generation_order=codex_generation_order(
                    str(expected.generation)
                ),
                plan=saved.plan,
            )
        except TypeError, ValueError:
            return self._persist_failure(
                account,
                CodexManagedOutcome.REJECTED,
                health=CredentialHealth.RECONCILIATION_REQUIRED,
            )
        if expected.account_id != account_id or not saved.not_older_than(
            selected
        ):
            return self._persist_failure(
                account,
                CodexManagedOutcome.REJECTED,
                health=CredentialHealth.RECONCILIATION_REQUIRED,
            )
        current = self._snapshot(account_id)
        if isinstance(current, ProviderFailure):
            return self._persist_provider_failure(account, current)
        if not current.not_older_than(saved):
            return self._persist_failure(
                account,
                CodexManagedOutcome.REJECTED,
                health=CredentialHealth.RECONCILIATION_REQUIRED,
            )
        return self._verified_exchange(
            account,
            saved,
            current,
            CodexAccountObservation(current.plan),
            refreshed=current.advanced_from(saved),
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
            return self.install_projection_with_authority(
                account_id,
                authority,
                installer,
            )

    def install_projection_with_authority(
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

    def _prepare_with_authority(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
        *,
        refresh_token: bool,
        process_group: CodexProcessGroupPolicy = (
            CodexProcessGroupPolicy.ISOLATED
        ),
    ) -> CodexVerifiedAuthorityExchange | CodexManagedAuthorityResult:
        authority.require(account_id)
        account = self._saved_account(account_id)
        return self._prepare_account(
            account,
            refresh_token=refresh_token,
            process_group=process_group,
        )

    def _prepare_expected_projection(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
        expected: CodexProjectionExpectation,
        *,
        refresh_token: bool,
    ) -> CodexVerifiedAuthorityExchange | CodexManagedAuthorityResult:
        authority.require(account_id)
        account = self._saved_account(account_id)
        saved = self._expected_snapshot(account)
        if isinstance(saved, ProviderFailure):
            return self._persist_provider_failure(account, saved)
        if (
            expected.account_id != account_id
            or expected.provider_identity != saved.provider_identity
            or expected.generation != saved.generation
        ):
            return self._persist_failure(
                account,
                CodexManagedOutcome.REJECTED,
                health=CredentialHealth.RECONCILIATION_REQUIRED,
            )
        return self._prepare_account(
            account,
            refresh_token=refresh_token,
            process_group=CodexProcessGroupPolicy.INHERITED,
        )

    def _prepare_account(
        self,
        account: SavedAccount,
        *,
        refresh_token: bool,
        process_group: CodexProcessGroupPolicy,
    ) -> CodexVerifiedAuthorityExchange | CodexManagedAuthorityResult:
        expected = self._expected_snapshot(account)
        if isinstance(expected, ProviderFailure):
            return self._persist_provider_failure(account, expected)
        before = self._snapshot(account.account_id)
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
            process_group=process_group,
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
        process_group: CodexProcessGroupPolicy,
    ) -> CodexVerifiedAuthorityExchange | CodexManagedAuthorityResult:
        try:
            session = self._home.open_session(
                account.account_id,
                process_group=process_group,
            )
        except CodexAppServerError as error:
            return self._persist_failure(
                account,
                _APP_SERVER_OUTCOMES[error.code],
            )
        observed: CodexAccountObservation | ProviderFailure | None = None
        app_error: CodexAppServerError | None = None
        try:
            with session:
                account_read = read_codex_account(
                    session,
                    refresh_token=refresh_token,
                )
                observed = (
                    codex_account_provider_failure(account_read)
                    if isinstance(account_read, CodexAccountReadFailure)
                    else account_read
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
    ) -> CodexVerifiedAuthorityExchange | CodexManagedAuthorityResult:
        if (
            after.provider_identity != before.provider_identity
            or after.provider_identity
            != require_managed_codex_authority(account).provider_identity
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
        return self._verified_exchange(
            account,
            before,
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
        authority = require_managed_codex_authority(account)
        try:
            order = codex_generation_order(str(authority.generation))
        except ValueError:
            return ProviderFailure(
                provider_id=ProviderId.CODEX,
                kind=ProviderFailureKind.MALFORMED,
                message=(
                    "The managed Codex credential generation is malformed."
                ),
            )
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
        require_managed_codex_authority(account)
        return account

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

    def _verified_exchange(
        self,
        account: SavedAccount,
        before: CodexAuthSnapshot,
        after: CodexAuthSnapshot,
        observed: CodexAccountObservation,
        *,
        refreshed: bool,
    ) -> CodexVerifiedAuthorityExchange:
        return CodexVerifiedAuthorityExchange(
            account,
            before,
            after,
            observed,
            refreshed,
        )

    def _persist_exchange(
        self,
        exchange: CodexVerifiedAuthorityExchange,
    ) -> CodexManagedAuthorityResult:
        previous = require_managed_codex_authority(exchange.source)
        verified_at = self._clock.now()
        candidate = managed_codex_account(
            exchange.source,
            previous.authority_id,
            exchange.after,
            plan=exchange.observation.plan,
            executable_version=str(self._capabilities.executable.version),
            verified_at=verified_at,
            refreshed=exchange.refreshed,
        )
        self._store.persist_state(
            candidate,
            expected=exchange.source,
        )
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
        action_required=outcome.action_required,
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

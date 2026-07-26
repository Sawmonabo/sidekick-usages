"""Current-only account persistence composition and administration."""

from collections.abc import Callable

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.index import AccountIndexReader
from sidekick_usages.persistence.accounts.removal.store import (
    AccountRemovalStore,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.credentials.refresh.artifacts import (
    CredentialRefreshArtifacts,
    CredentialRefreshState,
)
from sidekick_usages.persistence.credentials.refresh.service import (
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.errors import (
    ResetIncompleteError,
    SupervisorActiveError,
)
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.models.status import (
    PermissionRepairResult,
    PersistenceStatus,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.types.credential import (
    PrivateCredentialState,
)
from sidekick_usages.persistence.types.status import PersistenceState

type MaintenanceQuiescent = Callable[[], bool]


class PersistenceService:
    """Compose and administer the sole current account store."""

    def __init__(
        self,
        paths: ApplicationPaths,
        *,
        maintenance_quiescent: MaintenanceQuiescent,
    ) -> None:
        self.paths = paths
        self._maintenance_quiescent = maintenance_quiescent
        self._filesystem = PersistenceFilesystem(paths.accounts)
        self._private = PrivateCredentialTree(
            paths.private_credentials,
            account_path=paths.accounts,
        )
        self._managed_codex = PrivateCredentialTree(
            paths.private_codex_profiles,
            account_path=paths.accounts,
        )
        self._managed_claude = PrivateCredentialTree(
            paths.private_claude_profiles,
            account_path=paths.accounts,
        )
        self._refresh = CredentialRefreshArtifacts(paths.credential_refresh)

    @property
    def private_credentials(self) -> PrivateCredentialTree:
        """Return the shared protected credential boundary."""
        return self._private

    @property
    def managed_codex_profiles(self) -> PrivateCredentialTree:
        """Return the stable private Codex-home boundary."""
        return self._managed_codex

    @property
    def managed_claude_profiles(self) -> PrivateCredentialTree:
        """Return the stable private Claude-profile boundary."""
        return self._managed_claude

    def open_store(self) -> AccountStore:
        """Return one recovered and loaded current account store."""
        return AccountStore(self.paths.accounts, self._private).load()

    def status(self, store: AccountStore | None = None) -> PersistenceStatus:
        """Return current secret-free store status."""
        current = self.open_store() if store is None else store
        return self._status(
            present=self._filesystem.read_authority() is not None,
            account_count=len(current),
        )

    def observe_accounts(
        self,
    ) -> tuple[PersistenceStatus, tuple[SavedAccount, ...]]:
        """Passively read account metadata and its matching store status."""
        document = AccountIndexReader(self.paths.accounts).observe()
        accounts = () if document is None else document.accounts
        return (
            self._status(
                present=document is not None,
                account_count=len(accounts),
            ),
            accounts,
        )

    def refresh_status(self) -> CredentialRefreshState:
        """Return passive private refresh-transaction status."""
        return self._refresh.assess()

    def reset_all(self) -> int:
        """Delete global state after provider-aware account retirement."""
        self._require_maintenance_quiescence()
        with self._refresh.hold_quiescent():
            self._require_maintenance_quiescence()
            store = self.open_store()
            CredentialRefreshTransactions(
                store,
                self.paths.credential_refresh,
            ).recover()
            if len(store):
                raise ResetIncompleteError(self.paths.accounts.name)
            removals = AccountRemovalStore(self.paths.durable_operations)
            if removals.load():
                raise ResetIncompleteError(removals.path.name)
            for profiles in (self._managed_claude, self._managed_codex):
                if profiles.observe() is not PrivateCredentialState.ABSENT:
                    raise ResetIncompleteError(profiles.root.name)
            self._refresh.destroy_all()
            self._private.destroy_all()
            return 0

    def repair_permissions(self) -> PermissionRepairResult:
        """Repair current Sidekick-owned credential permissions."""
        self._require_maintenance_quiescence()
        repair = self._private.repair_permissions(
            locked_precondition=self._require_maintenance_quiescence,
        )
        return PermissionRepairResult(repair, self.status())

    def _require_maintenance_quiescence(self) -> None:
        if not self._maintenance_quiescent():
            raise SupervisorActiveError

    def _status(
        self,
        *,
        present: bool,
        account_count: int,
    ) -> PersistenceStatus:
        state = (
            PersistenceState.CURRENT if present else PersistenceState.EMPTY
        )
        return PersistenceStatus(state, self.paths.accounts, account_count)

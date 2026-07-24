"""Current-only account persistence composition and administration."""

from collections.abc import Callable

from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.credential_refresh import (
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.credential_refresh_artifacts import (
    CredentialRefreshArtifacts,
    CredentialRefreshState,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.models.status import (
    PermissionRepairResult,
    PersistenceStatus,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.types.status import PersistenceState
from sidekick_usages.scheduler_quiescence import (
    SchedulerMutationBlockedError,
    SchedulerQuiescenceAssessment,
)

type SchedulerAssessor = Callable[[], SchedulerQuiescenceAssessment]


class PersistenceService:
    """Compose and administer the sole current account store."""

    def __init__(
        self,
        paths: ApplicationPaths,
        *,
        scheduler_assessor: SchedulerAssessor,
    ) -> None:
        self.paths = paths
        self._scheduler_assessor = scheduler_assessor
        self._filesystem = PersistenceFilesystem(paths.accounts)
        self._private = PrivateCredentialTree(
            paths.private_credentials,
            account_path=paths.accounts,
        )
        self._refresh = CredentialRefreshArtifacts(paths.credential_refresh)

    @property
    def private_credentials(self) -> PrivateCredentialTree:
        """Return the shared protected credential boundary."""
        return self._private

    def open_store(self) -> AccountStore:
        """Return one recovered and loaded current account store."""
        return AccountStore(self.paths.accounts, self._private).load()

    def status(self, store: AccountStore | None = None) -> PersistenceStatus:
        """Return current secret-free store status."""
        current = self.open_store() if store is None else store
        authority = self._filesystem.read_authority()
        state = (
            PersistenceState.EMPTY
            if authority is None
            else PersistenceState.CURRENT
        )
        return PersistenceStatus(state, self.paths.accounts, len(current))

    def refresh_status(self) -> CredentialRefreshState:
        """Return passive private refresh-transaction status."""
        return self._refresh.assess()

    def reset_all(self) -> int:
        """Delete all Sidekick account and credential state."""
        self._require_scheduler_quiescence()
        with self._refresh.hold_quiescent():
            self._require_scheduler_quiescence()
            store = self.open_store()
            CredentialRefreshTransactions(
                store,
                self.paths.credential_refresh,
            ).recover()
            removed = store.reset_all()
            self._refresh.destroy_all()
            self._private.destroy_all()
            return removed

    def repair_permissions(self) -> PermissionRepairResult:
        """Repair current Sidekick-owned credential permissions."""
        self._require_scheduler_quiescence()
        repair = self._private.repair_permissions(
            locked_precondition=self._require_scheduler_quiescence,
        )
        return PermissionRepairResult(repair, self.status())

    def _require_scheduler_quiescence(self) -> None:
        assessment = self._scheduler_assessor()
        if not assessment.quiescent:
            raise SchedulerMutationBlockedError(assessment)

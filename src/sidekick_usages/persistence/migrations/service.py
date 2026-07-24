"""Public persistence migration coordinator and composition boundary."""

from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from sidekick_usages.core.models import Account
from sidekick_usages.paths import (
    ApplicationPaths,
)
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.artifacts import (
    AuthorityGeneration,
    FileSnapshot,
)
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceCompositionFailure,
    PersistenceOperationResult,
)
from sidekick_usages.persistence.credential_refresh import (
    CredentialRefreshArtifacts,
    CredentialRefreshRecoveryBlockedError,
    CredentialRefreshState,
    CredentialRefreshStateKind,
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.credential_transactions import (
    CredentialSourceGuard,
    PrivateCredentialTransaction,
)
from sidekick_usages.persistence.errors import (
    PersistenceError,
    SourceChangedError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.managed_migration import (
    ManagedAccountMigrationService,
)
from sidekick_usages.persistence.migrations.account import (
    AccountFilesystemFactory,
    AccountLockFactory,
    AccountMigrationCoordinator,
    MigrationTransaction,
    PermissionRepairOperationResult,
    PrivateCredentialArtifacts,
    ReleasedV060Verifier,
)
from sidekick_usages.persistence.migrations.account_preview import (
    AccountMigrationPreview,
)
from sidekick_usages.persistence.migrations.errors import (
    LocationMigrationStateError,
    SchedulerMutationBlockedError,
)
from sidekick_usages.persistence.migrations.location import (
    CanonicalSelection,
    CompatibilitySelection,
    ConflictSelection,
    EquivalentSelection,
    LocationMigrationAssessment,
    LocationMigrationPlan,
    LocationMigrationResult,
    LocationRole,
    PartialSelection,
    ReadyLocationSelection,
    RuntimePersistenceSelection,
    completed_location_assessment,
    is_ready_location_selection,
    location_migration_plan,
    ready_location_assessment,
)
from sidekick_usages.persistence.migrations.location_state import (
    HeldLocationState as _HeldLocationState,
)
from sidekick_usages.persistence.migrations.location_state import (
    LocationMigrationWork as _LocationMigrationWork,
)
from sidekick_usages.persistence.migrations.location_state import (
    RuntimePersistence,
)
from sidekick_usages.persistence.migrations.location_state import (
    operation_role as _operation_role,
)
from sidekick_usages.persistence.migrations.location_state import (
    path_text as _path_text,
)
from sidekick_usages.persistence.migrations.location_state import (
    ready_role as _ready_role,
)
from sidekick_usages.persistence.migrations.observer import (
    LocationEvidence,
    LocationObserver,
    PrivateCredentialTreeFactory,
)
from sidekick_usages.persistence.migrations.ports import (
    PreparedPrivateAuthMigration,
    PrivateAuthMigrator,
)
from sidekick_usages.persistence.migrations.rollback import (
    PersistenceRollbackService,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.account import (
    SCHEMA_VERSION as MANAGED_SCHEMA_VERSION,
)
from sidekick_usages.persistence.schemas import (
    encode_version_two,
)
from sidekick_usages.persistence.transforms import (
    accounts_to_version_two,
)
from sidekick_usages.scheduler_quiescence import (
    SchedulerQuiescenceAssessment,
)


class PersistenceMigrationService:
    """Coordinate explicit account and location persistence transitions."""

    def __init__(
        self,
        paths: ApplicationPaths,
        *,
        scheduler_assessor: Callable[[], SchedulerQuiescenceAssessment],
        private_auth_migrator: PrivateAuthMigrator,
        released_v060_verifier: ReleasedV060Verifier,
        filesystem_factory: AccountFilesystemFactory = PersistenceFilesystem,
        lock_factory: AccountLockFactory = PersistenceLock,
        private_tree_factory: PrivateCredentialTreeFactory = (
            PrivateCredentialTree
        ),
    ) -> None:
        self.paths = paths
        self._scheduler_assessor = scheduler_assessor
        self._released_v060_verifier = released_v060_verifier
        self._filesystem_factory = filesystem_factory
        self._lock_factory = lock_factory
        self._refresh_artifacts = CredentialRefreshArtifacts(
            paths.credential_refresh
        )
        self._observer = LocationObserver(
            paths,
            private_auth_migrator=private_auth_migrator,
            filesystem_factory=filesystem_factory,
            private_tree_factory=private_tree_factory,
        )
        self._rollback = PersistenceRollbackService(
            paths,
            self._observer,
            scheduler_assessor=scheduler_assessor,
            released_v060_verifier=released_v060_verifier,
            filesystem_factory=filesystem_factory,
            lock_factory=lock_factory,
            hold_location_state=self._hold_location_state,
        )

    def assess_locations(
        self,
    ) -> LocationMigrationAssessment[RuntimePersistenceSelection]:
        """Return one complete passive location assessment."""
        return self._observer.assess()

    def location_migration_preview(
        self,
    ) -> LocationMigrationAssessment[RuntimePersistenceSelection]:
        """Require scheduler quiescence and return relocation evidence."""
        self._require_scheduler_quiescence()
        return self.assess_locations()

    def migrate_locations(
        self,
        *,
        replace_conflicting_destination: bool = False,
    ) -> LocationMigrationResult:
        """Relocate state while excluding every credential refresh."""
        if type(replace_conflicting_destination) is not bool:
            raise TypeError("replace_conflicting_destination must be Boolean.")
        with self._refresh_artifacts.hold_quiescent():
            self._resolve_refresh_transactions()
            return self._migrate_locations_quiescent(
                replace_conflicting_destination=(
                    replace_conflicting_destination
                )
            )

    def _migrate_locations_quiescent(
        self,
        *,
        replace_conflicting_destination: bool,
    ) -> LocationMigrationResult:
        """Relocate compatibility state through one bounded transaction."""
        self._require_scheduler_quiescence()
        preview = self.assess_locations()
        if completed := self._completed_location_result(preview):
            return completed
        if not (
            isinstance(
                preview.selection,
                (CompatibilitySelection, PartialSelection),
            )
            or (
                replace_conflicting_destination
                and isinstance(preview.selection, ConflictSelection)
            )
        ):
            raise LocationMigrationStateError(preview)

        with ExitStack() as stack:
            held = self._hold_location_state(stack)
            self._require_scheduler_quiescence()
            coordinator = PrivateCredentialTransaction(
                self._observer.tree(LocationRole.CANONICAL),
                held.canonical_filesystem.read_authority,
            )
            rebase_used = self._recover_location_transaction(
                coordinator,
                held,
            )
            recovered = self.assess_locations()
            if completed := self._completed_location_result(recovered):
                return completed
            work = self._prepare_location_work(
                held,
                replace_conflicting_destination=(
                    replace_conflicting_destination
                ),
            )
            try:
                self._commit_location_work(coordinator, held, work)
            except SourceChangedError:
                if rebase_used:
                    raise LocationMigrationStateError(
                        self.assess_locations()
                    ) from None
                if self._observer.tree(
                    LocationRole.CANONICAL
                ).transaction_directory_present():
                    self._resolve_location_divergence(
                        coordinator,
                        held,
                    )
                self._require_scheduler_quiescence()
                work = self._prepare_location_work(
                    held,
                    replace_conflicting_destination=(
                        replace_conflicting_destination
                    ),
                )
                try:
                    self._commit_location_work(coordinator, held, work)
                except SourceChangedError:
                    raise LocationMigrationStateError(
                        self.assess_locations()
                    ) from None
            final = self.assess_locations()
            completed = self._completed_location_result(
                final,
                plan=work.plan,
            )
            if completed is None:
                raise LocationMigrationStateError(final)
            return completed

    def runtime(self) -> RuntimePersistence:
        """Return the currently ready runtime authority and private tree."""
        observed = self.assess_locations()
        if not is_ready_location_selection(observed.selection):
            raise LocationMigrationStateError(observed)
        assessment = ready_location_assessment(observed)
        role = _ready_role(assessment.selection)
        return RuntimePersistence(
            locations=self._observer.account_locations(role),
            private_credentials=self._observer.tree(role),
            assessment=assessment,
        )

    def require_location_unchanged(
        self,
        expected: LocationMigrationAssessment[ReadyLocationSelection],
    ) -> None:
        """Fail when runtime location evidence changed after a read."""
        current = self.assess_locations()
        if current != expected:
            raise LocationMigrationStateError(current)

    def assess(self) -> PersistenceAssessment:
        """Return the selected account authority's passive assessment."""
        assessment = self.assess_locations()
        role = _operation_role(assessment)
        return self._account(role).assess()

    def assess_refresh(self) -> CredentialRefreshState:
        """Return passive secret-free private refresh state."""
        return self._refresh_artifacts.assess()

    def mutation_preview(self) -> PersistenceAssessment:
        """Require quiescence and return an account mutation preview."""
        assessment = self.assess_locations()
        role = _operation_role(assessment)
        return self._account(role).mutation_preview()

    def account_migration_preview(self) -> AccountMigrationPreview:
        """Return one read-only account migration credential preview."""
        assessment = self.assess_locations()
        role = _operation_role(assessment)
        return self._account(role).account_migration_preview()

    def permission_repair_preview(
        self,
    ) -> PersistenceAssessment | PersistenceCompositionFailure:
        """Return repair scope even when unsafe permissions block it."""
        assessment = self.assess_locations()
        role = _operation_role(assessment)
        return self._account(role).permission_repair_preview()

    def repair_permissions(self) -> PermissionRepairOperationResult:
        """Repair a selected released layout and reassess it."""
        assessment = self.assess_locations()
        role = _operation_role(assessment)
        return self._account(role).repair_permissions()

    def read_accounts(self) -> tuple[Account, ...]:
        """Return a stable validated snapshot from a ready location."""
        observed = self.assess_locations()
        if not is_ready_location_selection(observed.selection):
            raise LocationMigrationStateError(observed)
        before = ready_location_assessment(observed)
        role = _ready_role(before.selection)
        private = self._observer.tree(role)
        accounts = tuple(
            AccountStore(
                self._observer.account_locations(role),
                orphaned_credentials_observer=private.observe,
                private_credentials=private,
                filesystem_factory=self._filesystem_factory,
                lock_factory=self._lock_factory,
            ).load()
        )
        current = self.assess_locations()
        if current != before:
            raise LocationMigrationStateError(current)
        return accounts

    def migrate_accounts(
        self,
        *,
        reimport_prototype: bool = False,
    ) -> PersistenceAssessment:
        """Migrate selected account schema or import the prototype."""
        with self._refresh_artifacts.hold_quiescent():
            self._resolve_refresh_transactions()
            assessment = self.assess_locations()
            role = _operation_role(assessment)
            current = self._account(role).assess()
            if current.schema_version == MANAGED_SCHEMA_VERSION:
                return current
            self._account(role).migrate_accounts(
                reimport_prototype=reimport_prototype
            )
            self._require_scheduler_quiescence()
            ManagedAccountMigrationService(
                self._observer.account_path(role),
                self._observer.tree(role),
                filesystem_factory=self._filesystem_factory,
                lock_factory=self._lock_factory,
            ).migrate()
            return self._account(role).assess()

    def prepare_rollback(self) -> PersistenceOperationResult:
        """Prepare exact released-v0.6.0 compatibility."""
        self._rollback.preflight()
        with self._refresh_artifacts.hold_quiescent():
            self._rollback.preflight()
            self._resolve_refresh_transactions()
            return self._prepare_rollback_quiescent()

    def _prepare_rollback_quiescent(self) -> PersistenceOperationResult:
        """Prepare rollback while refresh lifecycle exclusion is held."""
        assessment = self.assess_locations()
        role = _operation_role(assessment)
        current = self._account(role).assess()
        if current.schema_version == MANAGED_SCHEMA_VERSION:
            return self._rollback.prepare_managed(role)
        if (
            role is LocationRole.CANONICAL
            and self.paths.accounts.canonical
            != self.paths.accounts.existing_sidekick
        ):
            return self._rollback.prepare_native(assessment)
        return self._account(role).prepare_rollback()

    def full_reset(self) -> PersistenceAssessment:
        """Delete every selected Sidekick-owned credential artifact."""
        with self._refresh_artifacts.hold_quiescent():
            self._resolve_refresh_transactions()
            assessment = self.assess_locations()
            role = _operation_role(assessment)
            self._refresh_artifacts.destroy_transactions()
            result = self._account(role).full_reset()
            if (
                self._refresh_artifacts.assess().kind
                is not CredentialRefreshStateKind.CLEAN
            ):
                raise CredentialRefreshRecoveryBlockedError
            return result

    def _resolve_refresh_transactions(self) -> None:
        """Resolve recoverable v2 evidence or block lifecycle mutation."""
        state = self._refresh_artifacts.assess()
        if state.kind is CredentialRefreshStateKind.CLEAN:
            return
        try:
            runtime = self.runtime()
            store = AccountStore(
                runtime.locations,
                orphaned_credentials_observer=(
                    runtime.private_credentials.observe
                ),
                private_credentials=runtime.private_credentials,
                filesystem_factory=self._filesystem_factory,
                lock_factory=self._lock_factory,
            ).load()
            CredentialRefreshTransactions(
                store,
                self.paths.credential_refresh,
            ).recover()
        except CredentialRefreshRecoveryBlockedError:
            raise
        except PersistenceError:
            raise CredentialRefreshRecoveryBlockedError from None
        if (
            self._refresh_artifacts.assess().kind
            is not CredentialRefreshStateKind.CLEAN
        ):
            raise CredentialRefreshRecoveryBlockedError

    def _hold_location_state(
        self,
        stack: ExitStack,
    ) -> _HeldLocationState:
        filesystems = {
            LocationRole.COMPATIBILITY: self._filesystem_factory(
                self.paths.accounts.existing_sidekick
            ),
            LocationRole.CANONICAL: self._filesystem_factory(
                self.paths.accounts.canonical
            ),
        }
        ordered = tuple(
            sorted(
                filesystems.items(),
                key=lambda item: str(
                    item[1].authority_path.resolve(strict=False)
                ),
            )
        )
        resolved = tuple(
            filesystem.authority_path.resolve(strict=False)
            for _role, filesystem in ordered
        )
        if len(resolved) != len(set(resolved)):
            raise LocationMigrationStateError(self.assess_locations())
        transactions: dict[LocationRole, MigrationTransaction] = {}
        for role, filesystem in ordered:
            transactions[role] = stack.enter_context(
                self._lock_factory(filesystem).hold()
            )
        return _HeldLocationState(
            compatibility_filesystem=filesystems[LocationRole.COMPATIBILITY],
            canonical_filesystem=filesystems[LocationRole.CANONICAL],
            compatibility_transaction=transactions[LocationRole.COMPATIBILITY],
            canonical_transaction=transactions[LocationRole.CANONICAL],
        )

    def _prepare_location_work(
        self,
        held: _HeldLocationState,
        *,
        replace_conflicting_destination: bool,
    ) -> _LocationMigrationWork:
        evidence: tuple[LocationEvidence, ...] = self._observer.observe()
        assessment = self._observer.assess_evidence(evidence)
        if not (
            isinstance(
                assessment.selection,
                (CompatibilitySelection, PartialSelection),
            )
            or (
                replace_conflicting_destination
                and isinstance(assessment.selection, ConflictSelection)
            )
        ):
            raise LocationMigrationStateError(assessment)
        source = self._observer.evidence_for_role(
            evidence,
            LocationRole.COMPATIBILITY,
        )
        if (
            source is None
            or source.authority_digest is None
            or not isinstance(
                source.private_auth,
                PreparedPrivateAuthMigration,
            )
        ):
            raise LocationMigrationStateError(assessment)
        source_snapshot = held.compatibility_filesystem.read_authority()
        if (
            source_snapshot is None
            or source_snapshot.fingerprint.digest != source.authority_digest
        ):
            raise SourceChangedError

        target = self._observer.evidence_for_role(
            evidence,
            LocationRole.CANONICAL,
        )
        target_snapshot = held.canonical_filesystem.read_authority()
        if target_snapshot is None:
            if target is not None:
                raise SourceChangedError
            target_accounts: tuple[Account, ...] = ()
        else:
            if (
                target is None
                or target.authority_digest is None
                or target_snapshot.fingerprint.digest
                != target.authority_digest
            ):
                raise SourceChangedError
            target_accounts = target.accounts

        plan = location_migration_plan(
            source.candidate,
            source.private_auth.assessment,
            self.paths.accounts.canonical,
        )
        rewritten = source.private_auth.accounts
        if (
            replace_conflicting_destination
            and isinstance(assessment.selection, ConflictSelection)
            and _has_stale_credential_replacement(
                rewritten,
                target_accounts,
            )
        ):
            raise LocationMigrationStateError(assessment)
        return _LocationMigrationWork(
            plan=plan,
            source=source_snapshot,
            target=target_snapshot,
            private_auth=source.private_auth,
            source_accounts=source.accounts,
            payload=encode_version_two(accounts_to_version_two(rewritten)),
            displaced_bundles=self._displaced_private_bundles(
                target_accounts,
                rewritten,
                LocationRole.CANONICAL,
            ),
        )

    def _commit_location_work(
        self,
        coordinator: PrivateCredentialTransaction,
        held: _HeldLocationState,
        work: _LocationMigrationWork,
    ) -> None:
        source_guard = self._credential_source_guard(
            LocationRole.COMPATIBILITY,
            held.compatibility_filesystem,
            work.source_accounts,
        )
        if (
            source_guard.expected
            != self._credential_source_guard(
                LocationRole.COMPATIBILITY,
                held.compatibility_filesystem,
                work.source_accounts,
            ).expected
        ):
            raise SourceChangedError
        if work.target is not None:
            held.canonical_transaction.publish_immutable(
                AuthorityGeneration.VERSION_TWO,
                work.target,
            )
        coordinator.commit_migration(
            held.canonical_transaction,
            AuthorityGeneration.VERSION_TWO,
            work.payload,
            work.expected_target,
            base_generation=work.base_generation,
            private_bundles=work.private_auth.private_bundles,
            displaced_bundles=work.displaced_bundles,
            source_guard=source_guard,
        )

    def _recover_location_transaction(
        self,
        coordinator: PrivateCredentialTransaction,
        held: _HeldLocationState,
    ) -> bool:
        tree = self._observer.tree(LocationRole.CANONICAL)
        if not tree.transaction_directory_present():
            return False
        source = self._observer.evidence_for_role(
            self._observer.observe(),
            LocationRole.COMPATIBILITY,
        )
        if source is None:
            raise LocationMigrationStateError(self.assess_locations())
        guard = self._credential_source_guard(
            LocationRole.COMPATIBILITY,
            held.compatibility_filesystem,
            source.accounts,
        )
        try:
            coordinator.recover(source_guard=guard)
        except SourceChangedError:
            coordinator.resolve_migration_source_divergence(
                held.canonical_transaction,
                source_guard=guard,
            )
            return True
        return False

    def _resolve_location_divergence(
        self,
        coordinator: PrivateCredentialTransaction,
        held: _HeldLocationState,
    ) -> None:
        source = self._observer.evidence_for_role(
            self._observer.observe(),
            LocationRole.COMPATIBILITY,
        )
        if source is None:
            raise LocationMigrationStateError(self.assess_locations())
        guard = self._credential_source_guard(
            LocationRole.COMPATIBILITY,
            held.compatibility_filesystem,
            source.accounts,
        )
        coordinator.resolve_migration_source_divergence(
            held.canonical_transaction,
            source_guard=guard,
        )

    def _completed_location_result(
        self,
        assessment: LocationMigrationAssessment[RuntimePersistenceSelection],
        *,
        plan: LocationMigrationPlan | None = None,
    ) -> LocationMigrationResult | None:
        selection = assessment.selection
        if not isinstance(
            selection, (CanonicalSelection, EquivalentSelection)
        ):
            return None
        if plan is None:
            compatibility = next(
                (
                    candidate
                    for candidate in assessment.candidates
                    if candidate.role is LocationRole.COMPATIBILITY
                ),
                None,
            )
            if compatibility is None:
                return None
            plan = location_migration_plan(
                compatibility,
                assessment.private_auth_summary,
                self.paths.accounts.canonical,
            )
        if isinstance(selection, CanonicalSelection):
            return LocationMigrationResult(
                plan,
                completed_location_assessment(selection, assessment),
            )
        return LocationMigrationResult(
            plan,
            completed_location_assessment(selection, assessment),
        )

    def _credential_source_guard(
        self,
        role: LocationRole,
        filesystem: PersistenceFilesystem,
        accounts: tuple[Account, ...],
    ) -> CredentialSourceGuard:
        def read() -> FileSnapshot | None:
            authority = filesystem.read_authority()
            if authority is None:
                return None
            return self._observer.source_guard_snapshot(
                authority,
                role,
                accounts,
            )

        expected = read()
        if expected is None:
            raise SourceChangedError
        return CredentialSourceGuard(
            filesystem.authority_path,
            expected.fingerprint,
            read,
        )

    def _displaced_private_bundles(
        self,
        before: tuple[Account, ...],
        after: tuple[Account, ...],
        role: LocationRole,
    ) -> tuple[Path, ...]:
        previous = self._observer.owned_private_homes(before, role)
        current = self._observer.owned_private_homes(after, role)
        return tuple(sorted(previous - current, key=_path_text))

    def _require_scheduler_quiescence(self) -> None:
        assessment = self._scheduler_assessor()
        if not assessment.quiescent:
            raise SchedulerMutationBlockedError(assessment)

    def _account(self, role: LocationRole) -> AccountMigrationCoordinator:
        return AccountMigrationCoordinator(
            self._observer.account_path(role),
            self.paths.accounts.prototype_cc_usage,
            scheduler_assessor=self._scheduler_assessor,
            private_credential_artifacts=self._observer.tree(role),
            released_v060_verifier=self._released_v060_verifier,
            filesystem_factory=self._filesystem_factory,
            lock_factory=self._lock_factory,
        )


def _has_stale_credential_replacement(
    source: tuple[Account, ...],
    destination: tuple[Account, ...],
) -> bool:
    """Return whether migration would roll back a newer credential."""
    destination_by_identity = {
        (account.provider_id, account.label): account
        for account in destination
    }
    for account in source:
        existing = destination_by_identity.get(
            (account.provider_id, account.label)
        )
        if (
            existing is not None
            and existing.credentials != account.credentials
            and existing.last_refresh_at is not None
            and account.last_refresh_at is not None
            and existing.last_refresh_at > account.last_refresh_at
        ):
            return True
    return False


__all__ = [
    "PermissionRepairOperationResult",
    "PersistenceMigrationService",
    "PrivateCredentialArtifacts",
    "ReleasedV060Verifier",
    "RuntimePersistence",
]

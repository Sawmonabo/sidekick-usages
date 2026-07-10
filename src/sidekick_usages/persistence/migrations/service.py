"""Public persistence migration coordinator and composition boundary."""

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from sidekick_usages.core.models import Account
from sidekick_usages.paths import (
    AccountLocations,
    ApplicationPaths,
)
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceCompositionFailure,
    PersistenceOperationResult,
    make_operation_result,
)
from sidekick_usages.persistence.credential_transactions import (
    CredentialSourceGuard,
    PrivateCredentialTransaction,
)
from sidekick_usages.persistence.errors import (
    PersistenceCode,
    PersistenceError,
    SourceChangedError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.migrations.account import (
    AccountFilesystemFactory,
    AccountLockFactory,
    AccountMigrationCoordinator,
    MigrationTransaction,
    PermissionRepairOperationResult,
    PrivateCredentialArtifacts,
    ReleasedV060Verifier,
)
from sidekick_usages.persistence.migrations.errors import (
    LocationMigrationStateError,
    PrivateAuthMigrationStateError,
    ReleasedVerifierBoundaryError,
    SchedulerMutationBlockedError,
    VerificationPhase,
)
from sidekick_usages.persistence.migrations.location import (
    CandidateBlockedSelection,
    CanonicalSelection,
    CompatibilitySelection,
    EmptySelection,
    EquivalentSelection,
    LocationMigrationAssessment,
    LocationMigrationPlan,
    LocationMigrationResult,
    LocationRole,
    PartialSelection,
    PrototypeSelection,
    ReadyLocationSelection,
    RuntimePersistenceSelection,
    completed_location_assessment,
    is_ready_location_selection,
    location_migration_plan,
    ready_location_assessment,
)
from sidekick_usages.persistence.migrations.observer import (
    LocationEvidence,
    LocationObserver,
    PrivateCredentialTreeFactory,
)
from sidekick_usages.persistence.migrations.ports import (
    PreparedPrivateAuthMigration,
    PrivateAuthMigrationFailure,
    PrivateAuthMigrator,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schemas import (
    encode_generation_zero,
    encode_version_one,
)
from sidekick_usages.persistence.transforms import (
    accounts_to_version_one,
    version_one_to_v060,
)
from sidekick_usages.scheduler_quiescence import (
    SchedulerQuiescenceAssessment,
)


@dataclass(frozen=True, slots=True)
class RuntimePersistence:
    """One ready runtime authority and its private credential boundary."""

    locations: AccountLocations
    private_credentials: PrivateCredentialTree = field(repr=False)
    assessment: LocationMigrationAssessment[ReadyLocationSelection]


@dataclass(frozen=True, slots=True)
class _HeldLocationState:
    compatibility_filesystem: PersistenceFilesystem = field(repr=False)
    canonical_filesystem: PersistenceFilesystem = field(repr=False)
    compatibility_transaction: MigrationTransaction = field(repr=False)
    canonical_transaction: MigrationTransaction = field(repr=False)


@dataclass(frozen=True, slots=True)
class _LocationMigrationWork:
    plan: LocationMigrationPlan
    source: FileSnapshot = field(repr=False)
    target: FileSnapshot | None = field(repr=False)
    private_auth: PreparedPrivateAuthMigration = field(repr=False)
    source_accounts: tuple[Account, ...] = field(repr=False)
    payload: bytes = field(repr=False)
    displaced_bundles: tuple[Path, ...] = field(repr=False)

    @property
    def expected_target(self) -> ExpectedAuthority:
        """Return the exact canonical base expectation."""
        if self.target is None:
            return AuthorityExpectation.ABSENT
        return self.target.fingerprint

    @property
    def base_generation(self) -> AuthorityGeneration | None:
        """Return the canonical base generation when present."""
        if self.target is None:
            return None
        return AuthorityGeneration.VERSION_ONE


@dataclass(frozen=True, slots=True)
class _RollbackTarget:
    snapshot: FileSnapshot | None = field(repr=False)
    expected: ExpectedAuthority
    base_generation: AuthorityGeneration | None


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
        self._observer = LocationObserver(
            paths,
            private_auth_migrator=private_auth_migrator,
            filesystem_factory=filesystem_factory,
            private_tree_factory=private_tree_factory,
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

    def migrate_locations(self) -> LocationMigrationResult:
        """Relocate compatibility state through one bounded transaction."""
        self._require_scheduler_quiescence()
        preview = self.assess_locations()
        if completed := self._completed_location_result(preview):
            return completed
        if not isinstance(
            preview.selection,
            (CompatibilitySelection, PartialSelection),
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
            work = self._prepare_location_work(held)
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
                work = self._prepare_location_work(held)
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

    def mutation_preview(self) -> PersistenceAssessment:
        """Require quiescence and return an account mutation preview."""
        assessment = self.assess_locations()
        role = _operation_role(assessment)
        return self._account(role).mutation_preview()

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
        accounts = self._account(_ready_role(before.selection)).read_accounts()
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
        assessment = self.assess_locations()
        role = _operation_role(assessment)
        return self._account(role).migrate_accounts(
            reimport_prototype=reimport_prototype
        )

    def prepare_rollback(self) -> PersistenceOperationResult:
        """Prepare exact released-v0.6.0 compatibility."""
        assessment = self.assess_locations()
        role = _operation_role(assessment)
        if (
            role is LocationRole.CANONICAL
            and self.paths.accounts.canonical
            != self.paths.accounts.existing_sidekick
        ):
            return self._prepare_native_rollback(assessment)
        return self._account(role).prepare_rollback()

    def full_reset(self) -> PersistenceAssessment:
        """Delete every selected Sidekick-owned credential artifact."""
        assessment = self.assess_locations()
        role = _operation_role(assessment)
        return self._account(role).full_reset()

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

    def _prepare_native_rollback(
        self,
        preview: LocationMigrationAssessment[RuntimePersistenceSelection],
    ) -> PersistenceOperationResult:
        if not isinstance(
            preview.selection,
            (CanonicalSelection, EquivalentSelection),
        ):
            raise LocationMigrationStateError(preview)
        self._require_scheduler_quiescence()
        self._verifier_preflight()
        with ExitStack() as stack:
            held = self._hold_location_state(stack)
            self._require_scheduler_quiescence()
            self._verifier_preflight()
            evidence = self._observer.observe()
            locked = self._observer.assess_evidence(evidence)
            source = self._observer.evidence_for_role(
                evidence,
                LocationRole.CANONICAL,
            )
            if source is None or source.authority_digest is None:
                raise LocationMigrationStateError(locked)
            source_snapshot = held.canonical_filesystem.read_authority()
            if (
                source_snapshot is None
                or source_snapshot.fingerprint.digest
                != source.authority_digest
            ):
                raise SourceChangedError
            prepared = self._observer.prepare_private_auth(
                source.accounts,
                source_role=LocationRole.CANONICAL,
                target_role=LocationRole.COMPATIBILITY,
            )
            if isinstance(prepared, PrivateAuthMigrationFailure):
                raise PrivateAuthMigrationStateError(prepared)
            if not isinstance(prepared, PreparedPrivateAuthMigration):
                raise TypeError(
                    "Private-auth migrator returned an invalid result."
                )

            target = self._rollback_target(held, evidence)
            if target.snapshot is not None:
                held.compatibility_transaction.publish_immutable(
                    AuthorityGeneration.VERSION_ONE,
                    target.snapshot,
                )

            rollback_document = accounts_to_version_one(prepared.accounts)
            rollback_version_one = encode_version_one(rollback_document)
            lineage = (
                held.compatibility_transaction.publish_migration_snapshot(
                    AuthorityGeneration.VERSION_ONE,
                    rollback_version_one,
                )
            )
            payload = encode_generation_zero(
                version_one_to_v060(rollback_document)
            )
            guard = self._credential_source_guard(
                LocationRole.CANONICAL,
                held.canonical_filesystem,
                source.accounts,
            )
            committed = PrivateCredentialTransaction(
                self._observer.tree(LocationRole.COMPATIBILITY),
                held.compatibility_filesystem.read_authority,
            ).commit_migration(
                held.compatibility_transaction,
                AuthorityGeneration.GENERATION_ZERO,
                payload,
                target.expected,
                base_generation=target.base_generation,
                private_bundles=prepared.private_bundles,
                displaced_bundles=(),
                source_guard=guard,
            )
            self._verifier_verify(
                self.paths.accounts.existing_sidekick,
                committed,
            )
            postcondition = self._account(LocationRole.COMPATIBILITY).assess()
            if postcondition.code is not PersistenceCode.ROLLBACK_PREPARED:
                raise LocationMigrationStateError(self.assess_locations())
            return make_operation_result(
                PersistenceCode.ROLLBACK_PREPARED,
                postcondition,
                artifact_basename=lineage.basename,
            )

    def _rollback_target(
        self,
        held: _HeldLocationState,
        evidence: tuple[LocationEvidence, ...],
    ) -> _RollbackTarget:
        observed = self._observer.evidence_for_role(
            evidence,
            LocationRole.COMPATIBILITY,
        )
        snapshot = held.compatibility_filesystem.read_authority()
        if snapshot is None:
            if observed is not None:
                raise SourceChangedError
            return _RollbackTarget(
                None,
                AuthorityExpectation.ABSENT,
                None,
            )
        if (
            observed is None
            or observed.authority_digest is None
            or snapshot.fingerprint.digest != observed.authority_digest
        ):
            raise SourceChangedError
        return _RollbackTarget(
            snapshot,
            snapshot.fingerprint,
            AuthorityGeneration.VERSION_ONE,
        )

    def _prepare_location_work(
        self,
        held: _HeldLocationState,
    ) -> _LocationMigrationWork:
        evidence: tuple[LocationEvidence, ...] = self._observer.observe()
        assessment = self._observer.assess_evidence(evidence)
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
        return _LocationMigrationWork(
            plan=plan,
            source=source_snapshot,
            target=target_snapshot,
            private_auth=source.private_auth,
            source_accounts=source.accounts,
            payload=encode_version_one(accounts_to_version_one(rewritten)),
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
        coordinator.commit_migration(
            held.canonical_transaction,
            AuthorityGeneration.VERSION_ONE,
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

    def _verifier_preflight(self) -> None:
        try:
            self._released_v060_verifier.preflight()
        except PersistenceError:
            raise
        except Exception:
            raise ReleasedVerifierBoundaryError(
                VerificationPhase.PREFLIGHT
            ) from None

    def _verifier_verify(
        self,
        path: Path,
        expected: FileSnapshot,
    ) -> None:
        try:
            self._released_v060_verifier.verify(path, expected)
        except PersistenceError:
            raise
        except Exception:
            raise ReleasedVerifierBoundaryError(
                VerificationPhase.POST_COMMIT
            ) from None

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


def _path_text(path: Path) -> str:
    return str(path)


def _ready_role(selection: ReadyLocationSelection) -> LocationRole:
    if isinstance(selection, EmptySelection):
        return LocationRole.CANONICAL
    if isinstance(selection, CompatibilitySelection):
        return LocationRole.COMPATIBILITY
    if isinstance(selection, (CanonicalSelection, EquivalentSelection)):
        return LocationRole.CANONICAL
    raise TypeError("Unknown ready location selection.")


def _operation_role(
    assessment: LocationMigrationAssessment[RuntimePersistenceSelection],
) -> LocationRole:
    selection = assessment.selection
    if is_ready_location_selection(selection):
        return _ready_role(selection)
    if isinstance(selection, PrototypeSelection):
        return LocationRole.CANONICAL
    if isinstance(selection, CandidateBlockedSelection):
        if selection.candidate.role is LocationRole.PROTOTYPE:
            return LocationRole.CANONICAL
        return selection.candidate.role
    raise LocationMigrationStateError(assessment)


__all__ = [
    "PermissionRepairOperationResult",
    "PersistenceMigrationService",
    "PrivateCredentialArtifacts",
    "ReleasedV060Verifier",
    "RuntimePersistence",
]

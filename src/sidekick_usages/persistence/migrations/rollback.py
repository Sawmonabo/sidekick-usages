"""Released rollback coordination for legacy and managed account schemas."""

from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

from sidekick_usages.core.models import Account
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.account_schema_v3 import (
    SCHEMA_VERSION as MANAGED_SCHEMA_VERSION,
)
from sidekick_usages.persistence.account_schema_v3 import (
    VersionThreeDocument,
    decode_version_three,
)
from sidekick_usages.persistence.account_store_v3 import ManagedAccountStore
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
    FileFingerprint,
    FileSnapshot,
    sha256_digest,
)
from sidekick_usages.persistence.assessment import (
    PersistenceOperationResult,
    make_operation_result,
)
from sidekick_usages.persistence.credential_authorities import (
    CredentialAuthorityRepository,
)
from sidekick_usages.persistence.credential_transactions import (
    CredentialSourceGuard,
    PrivateCredentialTransaction,
)
from sidekick_usages.persistence.errors import (
    PersistenceCode,
    SourceChangedError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.managed_rollback import (
    authority_bundle_paths,
    guarded_legacy_source,
    require_v060_compatible,
)
from sidekick_usages.persistence.migrations.account import (
    AccountFilesystemFactory,
    AccountLockFactory,
    AccountMigrationCoordinator,
    ReleasedV060Verifier,
)
from sidekick_usages.persistence.migrations.errors import (
    LocationMigrationStateError,
    PrivateAuthMigrationStateError,
    SchedulerMutationBlockedError,
)
from sidekick_usages.persistence.migrations.location import (
    CanonicalSelection,
    EquivalentSelection,
    LocationMigrationAssessment,
    LocationRole,
    RuntimePersistenceSelection,
)
from sidekick_usages.persistence.migrations.location_state import (
    HeldLocationState as _HeldLocationState,
)
from sidekick_usages.persistence.migrations.location_state import (
    RollbackTarget as _RollbackTarget,
)
from sidekick_usages.persistence.migrations.location_state import (
    operation_role,
)
from sidekick_usages.persistence.migrations.observer import (
    LocationEvidence,
    LocationObserver,
)
from sidekick_usages.persistence.migrations.ports import (
    PreparedPrivateAuthMigration,
    PrivateAuthMigrationFailure,
)
from sidekick_usages.persistence.migrations.released_verification import (
    verifier_preflight,
    verifier_verify,
)
from sidekick_usages.persistence.schemas import (
    CURRENT_SCHEMA_VERSION,
    encode_generation_zero,
    encode_version_two,
)
from sidekick_usages.persistence.transforms import (
    accounts_to_version_two,
    version_two_to_v060,
)
from sidekick_usages.scheduler_quiescence import (
    SchedulerQuiescenceAssessment,
)

type _HoldLocationState = Callable[[ExitStack], _HeldLocationState]


class PersistenceRollbackService:
    """Prepare exact released-v0.6 compatibility across account layouts."""

    def __init__(
        self,
        paths: ApplicationPaths,
        observer: LocationObserver,
        *,
        scheduler_assessor: Callable[[], SchedulerQuiescenceAssessment],
        released_v060_verifier: ReleasedV060Verifier,
        filesystem_factory: AccountFilesystemFactory,
        lock_factory: AccountLockFactory,
        hold_location_state: _HoldLocationState,
    ) -> None:
        self.paths = paths
        self._observer = observer
        self._scheduler_assessor = scheduler_assessor
        self._released_v060_verifier = released_v060_verifier
        self._filesystem_factory = filesystem_factory
        self._lock_factory = lock_factory
        self._hold_location_state = hold_location_state

    def preflight(self) -> None:
        """Reject managed provider authorities before rollback mutation."""
        assessment = self._observer.assess()
        role = self._operation_role(assessment)
        current = self._account(role).assess()
        if current.schema_version != MANAGED_SCHEMA_VERSION:
            return
        filesystem = self._filesystem_factory(
            self._observer.account_path(role)
        )
        source = filesystem.read_authority()
        if source is None:
            raise SourceChangedError
        require_v060_compatible(decode_version_three(source.data))

    def prepare_managed(
        self,
        role: LocationRole,
    ) -> PersistenceOperationResult:
        """Convert legacy authorities while retaining exact v3 lineage."""
        if (
            role is LocationRole.CANONICAL
            and self.paths.accounts.canonical
            != self.paths.accounts.existing_sidekick
        ):
            return self._prepare_managed_native()
        return self._prepare_managed_in_place(role)

    def _prepare_managed_in_place(
        self,
        role: LocationRole,
    ) -> PersistenceOperationResult:
        self._require_scheduler_quiescence()
        verifier_preflight(self._released_v060_verifier)
        filesystem = self._filesystem_factory(
            self._observer.account_path(role)
        )
        tree = self._observer.tree(role)
        repository = CredentialAuthorityRepository(tree)
        with self._lock_factory(filesystem).hold() as transaction:
            self._require_scheduler_quiescence()
            source = filesystem.read_authority()
            if source is None:
                raise SourceChangedError
            document = decode_version_three(source.data)
            require_v060_compatible(document)
            verifier_preflight(self._released_v060_verifier)
            coordinator = PrivateCredentialTransaction(
                tree,
                filesystem.read_authority,
            )
            coordinator.recover()
            source = filesystem.read_authority()
            if source is None:
                raise SourceChangedError
            document, accounts = self._managed_runtime_accounts(
                role,
                filesystem,
                source,
            )
            payload = encode_generation_zero(
                version_two_to_v060(accounts_to_version_two(accounts))
            )
            lineage = transaction.publish_immutable(
                AuthorityGeneration.VERSION_THREE,
                source,
            )
            committed = coordinator.commit(
                transaction,
                payload,
                source.fingerprint,
                target_generation=AuthorityGeneration.GENERATION_ZERO,
                private_bundles=(),
                displaced_bundles=authority_bundle_paths(
                    document,
                    repository,
                ),
            )
            verifier_verify(
                self._released_v060_verifier,
                filesystem.authority_path,
                committed,
            )
            postcondition = self._account(role).assess()
            if postcondition.code is not PersistenceCode.ROLLBACK_PREPARED:
                raise LocationMigrationStateError(self._observer.assess())
            return make_operation_result(
                PersistenceCode.ROLLBACK_PREPARED,
                postcondition,
                artifact_basename=lineage.basename,
            )

    def _prepare_managed_native(self) -> PersistenceOperationResult:
        preview = self._observer.assess()
        if not isinstance(
            preview.selection,
            (CanonicalSelection, EquivalentSelection),
        ):
            raise LocationMigrationStateError(preview)
        self._require_scheduler_quiescence()
        verifier_preflight(self._released_v060_verifier)
        with ExitStack() as stack:
            held = self._hold_location_state(stack)
            self._require_scheduler_quiescence()
            source = held.canonical_filesystem.read_authority()
            if source is None:
                raise SourceChangedError
            document = decode_version_three(source.data)
            require_v060_compatible(document)
            verifier_preflight(self._released_v060_verifier)
            source_coordinator = PrivateCredentialTransaction(
                self._observer.tree(LocationRole.CANONICAL),
                held.canonical_filesystem.read_authority,
            )
            source_coordinator.recover()
            source = held.canonical_filesystem.read_authority()
            if source is None:
                raise SourceChangedError
            _document, accounts = self._managed_runtime_accounts(
                LocationRole.CANONICAL,
                held.canonical_filesystem,
                source,
            )
            prepared = self._observer.prepare_private_auth(
                accounts,
                source_role=LocationRole.CANONICAL,
                target_role=LocationRole.COMPATIBILITY,
            )
            if isinstance(prepared, PrivateAuthMigrationFailure):
                raise PrivateAuthMigrationStateError(prepared)
            if not isinstance(prepared, PreparedPrivateAuthMigration):
                raise TypeError(
                    "Private-auth migrator returned an invalid result."
                )
            source_guard = self._managed_source_guard(
                LocationRole.CANONICAL,
                held.canonical_filesystem,
            )
            target_coordinator = PrivateCredentialTransaction(
                self._observer.tree(LocationRole.COMPATIBILITY),
                held.compatibility_filesystem.read_authority,
            )
            target_coordinator.recover_migration(
                held.compatibility_transaction,
                source_guard=source_guard,
            )
            recovered = self._account(LocationRole.COMPATIBILITY).assess()
            if recovered.code is PersistenceCode.ROLLBACK_PREPARED:
                committed = held.compatibility_filesystem.read_authority()
                if committed is None:
                    raise SourceChangedError
                verifier_verify(
                    self._released_v060_verifier,
                    held.compatibility_filesystem.authority_path,
                    committed,
                )
                lineage = (
                    held.compatibility_transaction.publish_migration_snapshot(
                        AuthorityGeneration.VERSION_THREE,
                        source.data,
                    )
                )
                return make_operation_result(
                    PersistenceCode.ROLLBACK_PREPARED,
                    recovered,
                    artifact_basename=lineage.basename,
                )

            evidence = self._observer.observe()
            target_evidence = self._observer.evidence_for_role(
                evidence,
                LocationRole.COMPATIBILITY,
            )
            target_accounts = (
                target_evidence.accounts if target_evidence is not None else ()
            )
            target = self._managed_target(held, target_evidence)
            lineage = (
                held.compatibility_transaction.publish_migration_snapshot(
                    AuthorityGeneration.VERSION_THREE,
                    source.data,
                )
            )
            rollback_document = accounts_to_version_two(prepared.accounts)
            payload = encode_generation_zero(
                version_two_to_v060(rollback_document)
            )
            committed = target_coordinator.commit_migration(
                held.compatibility_transaction,
                AuthorityGeneration.GENERATION_ZERO,
                payload,
                target.expected,
                base_generation=target.base_generation,
                private_bundles=prepared.private_bundles,
                displaced_bundles=self._displaced_private_bundles(
                    target_accounts,
                    prepared.accounts,
                    LocationRole.COMPATIBILITY,
                ),
                source_guard=source_guard,
            )
            verifier_verify(
                self._released_v060_verifier,
                held.compatibility_filesystem.authority_path,
                committed,
            )
            postcondition = self._account(LocationRole.COMPATIBILITY).assess()
            if postcondition.code is not PersistenceCode.ROLLBACK_PREPARED:
                raise LocationMigrationStateError(self._observer.assess())
            return make_operation_result(
                PersistenceCode.ROLLBACK_PREPARED,
                postcondition,
                artifact_basename=lineage.basename,
            )

    def _managed_runtime_accounts(
        self,
        role: LocationRole,
        filesystem: PersistenceFilesystem,
        source: FileSnapshot,
    ) -> tuple[VersionThreeDocument, tuple[Account, ...]]:
        document = decode_version_three(source.data)
        require_v060_compatible(document)
        store = ManagedAccountStore(
            filesystem,
            self._observer.tree(role),
            filesystem.read_authority,
            lock_factory=self._lock_factory,
        )
        store.load(source)
        return document, tuple(store)

    def _managed_source_guard(
        self,
        role: LocationRole,
        filesystem: PersistenceFilesystem,
    ) -> CredentialSourceGuard:
        def read() -> FileSnapshot | None:
            source = filesystem.read_authority()
            if source is None:
                return None
            document, accounts = self._managed_runtime_accounts(
                role,
                filesystem,
                source,
            )
            protected = guarded_legacy_source(
                source,
                document,
                CredentialAuthorityRepository(self._observer.tree(role)),
            )
            provider = self._observer.source_guard_snapshot(
                source,
                role,
                accounts,
            )
            data = (
                b"sidekick-usages:managed-native-rollback-source:v1"
                + str(protected.fingerprint.digest).encode("ascii")
                + str(provider.fingerprint.digest).encode("ascii")
            )
            return FileSnapshot(
                FileFingerprint(
                    source.fingerprint.identity,
                    sha256_digest(data),
                    len(data),
                ),
                source.link_count,
                data,
            )

        expected = read()
        if expected is None:
            raise SourceChangedError
        return CredentialSourceGuard(
            filesystem.authority_path,
            expected.fingerprint,
            read,
        )

    def _managed_target(
        self,
        held: _HeldLocationState,
        evidence: LocationEvidence | None,
    ) -> _RollbackTarget:
        snapshot = held.compatibility_filesystem.read_authority()
        if snapshot is None:
            if evidence is not None:
                raise SourceChangedError
            return _RollbackTarget(
                None,
                AuthorityExpectation.ABSENT,
                None,
            )
        if (
            evidence is None
            or evidence.authority_digest is None
            or snapshot.fingerprint.digest != evidence.authority_digest
            or evidence.candidate.assessment.schema_version
            != CURRENT_SCHEMA_VERSION
        ):
            raise LocationMigrationStateError(self._observer.assess())
        return _RollbackTarget(
            snapshot,
            snapshot.fingerprint,
            AuthorityGeneration.VERSION_TWO,
        )

    def prepare_native(
        self,
        preview: LocationMigrationAssessment[RuntimePersistenceSelection],
    ) -> PersistenceOperationResult:
        """Prepare a distinct compatibility location from current v2."""
        if not isinstance(
            preview.selection,
            (CanonicalSelection, EquivalentSelection),
        ):
            raise LocationMigrationStateError(preview)
        self._require_scheduler_quiescence()
        verifier_preflight(self._released_v060_verifier)
        with ExitStack() as stack:
            held = self._hold_location_state(stack)
            self._require_scheduler_quiescence()
            verifier_preflight(self._released_v060_verifier)
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
                    AuthorityGeneration.VERSION_TWO,
                    target.snapshot,
                )

            rollback_document = accounts_to_version_two(prepared.accounts)
            rollback_version_two = encode_version_two(rollback_document)
            lineage = (
                held.compatibility_transaction.publish_migration_snapshot(
                    AuthorityGeneration.VERSION_TWO,
                    rollback_version_two,
                )
            )
            payload = encode_generation_zero(
                version_two_to_v060(rollback_document)
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
            verifier_verify(
                self._released_v060_verifier,
                self.paths.accounts.existing_sidekick,
                committed,
            )
            postcondition = self._account(LocationRole.COMPATIBILITY).assess()
            if postcondition.code is not PersistenceCode.ROLLBACK_PREPARED:
                raise LocationMigrationStateError(self._observer.assess())
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
            AuthorityGeneration.VERSION_TWO,
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
        return tuple(
            sorted(
                previous - current,
                key=lambda path: path.as_posix(),
            )
        )

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

    @staticmethod
    def _operation_role(
        assessment: LocationMigrationAssessment[RuntimePersistenceSelection],
    ) -> LocationRole:
        return operation_role(assessment)


__all__ = ["PersistenceRollbackService"]

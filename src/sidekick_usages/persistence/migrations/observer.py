"""Read-only persistence-location observation and assessment."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from sidekick_usages.core.models import Account
from sidekick_usages.paths import AccountLocations, ApplicationPaths
from sidekick_usages.persistence.artifacts import (
    FileFingerprint,
    FileSnapshot,
    Sha256Digest,
    sha256_digest,
)
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceIssue,
    assess_persistence,
)
from sidekick_usages.persistence.credential_transaction_schema import (
    MigrationCredentialTransactionJournal,
    decode_credential_journal,
)
from sidekick_usages.persistence.errors import (
    ManagedFileReadError,
    PersistenceCode,
    PersistenceError,
    SourceChangedError,
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.inventory import PersistenceInventory
from sidekick_usages.persistence.migrations.account import (
    AccountFilesystemFactory,
    accounts_from_current,
)
from sidekick_usages.persistence.migrations.account_codecs import (
    CURRENT_VERSION_TWO,
)
from sidekick_usages.persistence.migrations.location import (
    CandidateBlockedSelection,
    CompatibilitySelection,
    ConflictSelection,
    EmptySelection,
    LocationCandidate,
    LocationMigrationAssessment,
    LocationRole,
    PartialSelection,
    PrototypeSelection,
    RuntimePersistenceSelection,
    is_ready_location_selection,
    select_runtime_persistence,
)
from sidekick_usages.persistence.migrations.ports import (
    PreparedPrivateAuthMigration,
    PrivateAuthBundleSnapshot,
    PrivateAuthHomeKind,
    PrivateAuthMigrationAssessment,
    PrivateAuthMigrationFailure,
    PrivateAuthMigrationFailureCode,
    PrivateAuthMigrationRequest,
    PrivateAuthMigrationResult,
    PrivateAuthMigrator,
)
from sidekick_usages.persistence.observations import (
    ArtifactKind,
    ArtifactState,
    StoredGeneration,
)
from sidekick_usages.persistence.private_bundle_paths import (
    private_bundle_relative_components,
)
from sidekick_usages.persistence.private_credentials import (
    PRIVATE_TRANSACTION_JOURNAL,
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schemas import encode_version_two
from sidekick_usages.persistence.transforms import accounts_to_version_two

_MIGRATE_ACCOUNTS_COMMAND = (
    "sidekick-usages",
    "migrate",
    "accounts",
)
_MIGRATE_LOCATIONS_COMMAND = (
    "sidekick-usages",
    "migrate",
    "locations",
)
_AUTHORITATIVE_LOCATION_COUNT = 2


class PrivateCredentialTreeFactory(Protocol):
    """Construct one private tree for an exact observed location."""

    def __call__(
        self,
        root: Path,
        *,
        account_path: Path,
        existing_root: Path,
    ) -> PrivateCredentialTree:
        """Return a tree bound to one authority and ownership root."""


@dataclass(frozen=True, slots=True)
class LocationEvidence:
    """Complete read-only evidence for one persistence candidate."""

    candidate: LocationCandidate
    accounts: tuple[Account, ...] = field(repr=False)
    authority_digest: Sha256Digest | None = field(repr=False)
    private_auth: (
        PreparedPrivateAuthMigration
        | PrivateAuthMigrationAssessment
        | PrivateAuthMigrationFailure
    ) = field(repr=False)


type _TreeMap = Mapping[LocationRole, PrivateCredentialTree]


class LocationObserver:
    """Observe and reduce all persistence locations without writing."""

    def __init__(
        self,
        paths: ApplicationPaths,
        *,
        private_auth_migrator: PrivateAuthMigrator,
        filesystem_factory: AccountFilesystemFactory = PersistenceFilesystem,
        private_tree_factory: PrivateCredentialTreeFactory = (
            PrivateCredentialTree
        ),
    ) -> None:
        self.paths = paths
        self._private_auth_migrator = private_auth_migrator
        self._filesystem_factory = filesystem_factory
        self._trees = self._build_trees(private_tree_factory)

    def assess(
        self,
    ) -> LocationMigrationAssessment[RuntimePersistenceSelection]:
        """Return one complete passive location assessment."""
        return self.assess_evidence(self.observe())

    def assess_evidence(
        self,
        evidence: tuple[LocationEvidence, ...],
    ) -> LocationMigrationAssessment[RuntimePersistenceSelection]:
        """Reduce one complete evidence tuple to a closed assessment."""
        candidates = tuple(item.candidate for item in evidence)
        selection = select_runtime_persistence(candidates)
        if isinstance(selection, PartialSelection) and (
            self._migration_journal_present()
        ):
            selection = replace(selection, resumable_migration=True)
        return LocationMigrationAssessment(
            selection=selection,
            candidates=candidates,
            source=_selection_source(
                selection,
                self.paths.accounts.canonical,
            ),
            destination=self.paths.accounts.canonical,
            private_auth_summary=_private_summary(selection, evidence),
            artifact_basename=_artifact_basename(selection),
            issues=_location_issues(selection, evidence),
            write_blocked=not is_ready_location_selection(selection),
            next_command=_location_next_command(selection),
        )

    def _migration_journal_present(self) -> bool:
        tree = self.tree(LocationRole.CANONICAL)
        if not tree.transaction_directory_present():
            return False
        try:
            snapshot = tree.read_owned_file(
                tree.transaction_directory,
                PRIVATE_TRANSACTION_JOURNAL,
            )
            if snapshot is None:
                return False
            journal = decode_credential_journal(snapshot.data)
        except PersistenceError:
            return False
        return isinstance(journal, MigrationCredentialTransactionJournal)

    def observe(self) -> tuple[LocationEvidence, ...]:
        """Return all authoritative evidence or the sole prototype."""
        if (
            self.paths.accounts.canonical
            == self.paths.accounts.existing_sidekick
        ):
            roles = (LocationRole.CANONICAL,)
        else:
            roles = (
                LocationRole.COMPATIBILITY,
                LocationRole.CANONICAL,
            )
        evidence = tuple(
            candidate
            for role in roles
            if (candidate := self._authority_candidate(role)) is not None
        )
        if evidence:
            return evidence
        prototype = self._prototype_candidate()
        return () if prototype is None else (prototype,)

    @staticmethod
    def evidence_for_role(
        evidence: tuple[LocationEvidence, ...],
        role: LocationRole,
    ) -> LocationEvidence | None:
        """Return evidence for one exact role when present."""
        return next(
            (item for item in evidence if item.candidate.role is role),
            None,
        )

    def tree(self, role: LocationRole) -> PrivateCredentialTree:
        """Return the private tree bound to an authoritative role."""
        try:
            return self._trees[role]
        except KeyError:
            raise ValueError(
                "Prototype has no private credential tree."
            ) from None

    def account_locations(self, role: LocationRole) -> AccountLocations:
        """Return runtime account locations for one authoritative role."""
        return AccountLocations(
            canonical=self.account_path(role),
            existing_sidekick=self.paths.accounts.existing_sidekick,
            prototype_cc_usage=self.paths.accounts.prototype_cc_usage,
        )

    def account_path(self, role: LocationRole) -> Path:
        """Return the account authority path for one role."""
        if role is LocationRole.CANONICAL:
            return self.paths.accounts.canonical
        if role is LocationRole.COMPATIBILITY:
            return self.paths.accounts.existing_sidekick
        raise ValueError("Prototype has no runtime account authority.")

    def private_root(self, role: LocationRole) -> Path:
        """Return the private credential root for one role."""
        if role is LocationRole.CANONICAL:
            return self.paths.private_codex.canonical
        if role is LocationRole.COMPATIBILITY:
            return self.paths.private_codex.existing_sidekick
        raise ValueError("Prototype has no private credential root.")

    def owned_private_homes(
        self,
        accounts: tuple[Account, ...],
        role: LocationRole,
    ) -> set[Path]:
        """Return validated Sidekick-owned homes for one role."""
        root = self.private_root(role)
        homes: set[Path] = set()
        for account in accounts:
            if account.codex_home is None:
                continue
            home = Path(account.codex_home)
            try:
                relative = home.relative_to(root).as_posix()
                components = private_bundle_relative_components(relative)
            except ValueError:
                continue
            homes.add(root.joinpath(*components))
        return homes

    def source_guard_snapshot(
        self,
        authority: FileSnapshot,
        role: LocationRole,
        accounts: tuple[Account, ...],
    ) -> FileSnapshot:
        """Bind account and private-source evidence into one guard snapshot."""
        root = self.private_root(role)
        payload = bytearray(b"sidekick-usages:location-source-guard:v1")
        _append_guard_frame(
            payload,
            str(authority.fingerprint.digest).encode("ascii"),
        )
        for home in sorted(
            self.owned_private_homes(accounts, role),
            key=_path_text,
        ):
            relative = home.relative_to(root).as_posix()
            observed = self.tree(role).read_relative_bundle(relative)
            if observed is None:
                raise SourceChangedError
            _append_guard_frame(payload, relative.encode("utf-8"))
            for basename, snapshot in sorted(observed.items()):
                _append_guard_frame(payload, basename.encode("utf-8"))
                _append_guard_frame(
                    payload,
                    str(snapshot.fingerprint.digest).encode("ascii"),
                )
        data = bytes(payload)
        fingerprint = FileFingerprint(
            authority.fingerprint.identity,
            sha256_digest(data),
            len(data),
        )
        return FileSnapshot(fingerprint, authority.link_count, data)

    def _authority_candidate(
        self,
        role: LocationRole,
    ) -> LocationEvidence | None:
        path = self.account_path(role)
        tree = self.tree(role)
        try:
            observation = PersistenceInventory(
                path,
                self.paths.accounts.prototype_cc_usage,
                filesystem_factory=self._filesystem_factory,
            ).inspect_authority(tree.observe())
            assessment = assess_persistence(observation)
        except (
            ManagedFileReadError,
            UnsafeManagedFileError,
            UnsupportedFilesystemError,
        ) as error:
            assessment = _failure_assessment(path, error)
            observation = None
        if assessment.code is PersistenceCode.EMPTY:
            return None

        accounts: tuple[Account, ...] = ()
        account_digest: Sha256Digest | None = None
        private_digest: Sha256Digest | None = None
        private_auth: (
            PrivateAuthMigrationResult | PrivateAuthMigrationAssessment
        ) = PrivateAuthMigrationAssessment(())
        lineage_account_digests: frozenset[Sha256Digest] = frozenset()
        if assessment.code in CURRENT_VERSION_TWO and observation is not None:
            accounts = accounts_from_current(observation, assessment)
            private_auth = self.prepare_private_auth(
                accounts,
                source_role=LocationRole.COMPATIBILITY,
                target_role=LocationRole.CANONICAL,
            )
            rewritten = (
                private_auth.accounts
                if isinstance(private_auth, PreparedPrivateAuthMigration)
                else accounts
            )
            account_digest = _account_digest(rewritten)
            if isinstance(private_auth, PreparedPrivateAuthMigration):
                private_digest = private_auth.semantic_digest
            lineage_account_digests = frozenset(
                sha256_digest(artifact.content)
                for artifact in observation.artifacts
                if artifact.kind is ArtifactKind.V2_SNAPSHOT
                and artifact.state is ArtifactState.VALID
                and artifact.content is not None
            )
        candidate = LocationCandidate(
            role=role,
            path=path,
            assessment=assessment,
            account_digest=account_digest,
            private_auth_digest=private_digest,
            lineage_account_digests=lineage_account_digests,
        )
        authority_digest = (
            None
            if observation is None or observation.authority.content is None
            else sha256_digest(observation.authority.content)
        )
        return LocationEvidence(
            candidate,
            accounts,
            authority_digest,
            private_auth,
        )

    def _prototype_candidate(self) -> LocationEvidence | None:
        path = self.paths.accounts.canonical
        tree = self.tree(LocationRole.CANONICAL)
        observation = PersistenceInventory(
            path,
            self.paths.accounts.prototype_cc_usage,
            filesystem_factory=self._filesystem_factory,
        ).inspect(tree.observe())
        assessment = assess_persistence(observation)
        if assessment.code is PersistenceCode.EMPTY:
            return None
        prototype_assessment = replace(
            assessment,
            safe_path=self.paths.accounts.prototype_cc_usage,
        )
        candidate = LocationCandidate(
            role=LocationRole.PROTOTYPE,
            path=self.paths.accounts.prototype_cc_usage,
            assessment=prototype_assessment,
            account_digest=None,
            private_auth_digest=None,
        )
        return LocationEvidence(
            candidate,
            (),
            None,
            PrivateAuthMigrationAssessment(()),
        )

    def prepare_private_auth(
        self,
        accounts: tuple[Account, ...],
        *,
        source_role: LocationRole,
        target_role: LocationRole,
    ) -> PrivateAuthMigrationResult:
        """Prepare auth relocation between the two authoritative roles."""
        if {source_role, target_role} != {
            LocationRole.COMPATIBILITY,
            LocationRole.CANONICAL,
        }:
            raise ValueError(
                "Private-auth preparation requires authoritative roles."
            )
        try:
            bundles = self._private_snapshots(accounts)
        except (
            ManagedFileReadError,
            UnsafeManagedFileError,
            UnsupportedFilesystemError,
        ):
            return PrivateAuthMigrationFailure(
                code=PrivateAuthMigrationFailureCode.UNSAFE_HOME,
                message=(
                    "A Sidekick-owned private-auth bundle is unsafe or "
                    "unreadable."
                ),
                accounts=tuple(account.label for account in accounts),
            )
        return self._private_auth_migrator.prepare(
            PrivateAuthMigrationRequest(
                accounts=accounts,
                source_root=self.private_root(source_role),
                source_kind=_private_auth_kind(source_role),
                target_root=self.private_root(target_role),
                target_kind=_private_auth_kind(target_role),
                bundles=bundles,
            )
        )

    def _private_snapshots(
        self,
        accounts: tuple[Account, ...],
    ) -> tuple[PrivateAuthBundleSnapshot, ...]:
        paths: set[Path] = set()
        for account in accounts:
            if account.codex_home is None:
                continue
            home = Path(account.codex_home)
            owned = self._owned_relative(home)
            if owned is None:
                continue
            role, relative = owned
            paths.add(home)
            other = (
                LocationRole.CANONICAL
                if role is LocationRole.COMPATIBILITY
                else LocationRole.COMPATIBILITY
            )
            paths.add(self.private_root(other).joinpath(*relative))
        return tuple(
            self._snapshot(path)
            for path in sorted(
                paths,
                key=lambda candidate: candidate.as_posix(),
            )
        )

    def _snapshot(self, home: Path) -> PrivateAuthBundleSnapshot:
        owned = self._owned_relative(home)
        if owned is None:
            raise ValueError("Private-auth observation path is not owned.")
        role, relative = owned
        relative_text = Path(*relative).as_posix()
        observed = self.tree(role).read_relative_bundle(relative_text)
        return PrivateAuthBundleSnapshot(
            home=home,
            present=observed is not None,
            files=(
                {}
                if observed is None
                else {
                    basename: snapshot.data
                    for basename, snapshot in observed.items()
                }
            ),
        )

    def _owned_relative(
        self,
        home: Path,
    ) -> tuple[LocationRole, tuple[str, ...]] | None:
        for role in (LocationRole.CANONICAL, LocationRole.COMPATIBILITY):
            try:
                text = home.relative_to(self.private_root(role)).as_posix()
                relative = private_bundle_relative_components(text)
            except ValueError:
                continue
            return role, relative
        return None

    def _build_trees(
        self,
        factory: PrivateCredentialTreeFactory,
    ) -> _TreeMap:
        canonical = factory(
            self.paths.private_codex.canonical,
            account_path=self.paths.accounts.canonical,
            existing_root=self.paths.private_codex.existing_sidekick,
        )
        if (
            self.paths.private_codex.canonical
            == self.paths.private_codex.existing_sidekick
            and self.paths.accounts.canonical
            == self.paths.accounts.existing_sidekick
        ):
            compatibility = canonical
        else:
            compatibility = factory(
                self.paths.private_codex.existing_sidekick,
                account_path=self.paths.accounts.existing_sidekick,
                existing_root=self.paths.private_codex.existing_sidekick,
            )
        return {
            LocationRole.CANONICAL: canonical,
            LocationRole.COMPATIBILITY: compatibility,
        }


def _account_digest(accounts: tuple[Account, ...]) -> Sha256Digest:
    payload = encode_version_two(accounts_to_version_two(accounts))
    return sha256_digest(payload)


def _private_auth_kind(role: LocationRole) -> PrivateAuthHomeKind:
    if role is LocationRole.COMPATIBILITY:
        return PrivateAuthHomeKind.COMPATIBILITY
    if role is LocationRole.CANONICAL:
        return PrivateAuthHomeKind.CANONICAL
    raise ValueError("Prototype has no private-auth ownership kind.")


def _path_text(path: Path) -> str:
    return str(path)


def _append_guard_frame(payload: bytearray, value: bytes) -> None:
    payload.extend(len(value).to_bytes(8, "big"))
    payload.extend(value)


def _selection_source(
    selection: RuntimePersistenceSelection,
    canonical: Path,
) -> Path:
    if isinstance(selection, EmptySelection):
        return canonical
    if isinstance(selection, (ConflictSelection, PartialSelection)):
        return selection.candidates[0].path
    return selection.candidate.path


def _private_summary(
    selection: RuntimePersistenceSelection,
    evidence: tuple[LocationEvidence, ...],
) -> PrivateAuthMigrationAssessment | PrivateAuthMigrationFailure:
    selected = _selected_evidence(selection, evidence)
    failures = tuple(
        item.private_auth
        for item in selected
        if isinstance(item.private_auth, PrivateAuthMigrationFailure)
    )
    if failures:
        return failures[0]
    for item in reversed(selected):
        if isinstance(item.private_auth, PreparedPrivateAuthMigration):
            return item.private_auth.assessment
        if isinstance(item.private_auth, PrivateAuthMigrationAssessment):
            return item.private_auth
    return PrivateAuthMigrationAssessment(())


def _selected_evidence(
    selection: RuntimePersistenceSelection,
    evidence: tuple[LocationEvidence, ...],
) -> tuple[LocationEvidence, ...]:
    if isinstance(selection, EmptySelection):
        return ()
    candidates = (
        selection.candidates
        if isinstance(selection, (ConflictSelection, PartialSelection))
        else (selection.candidate,)
    )
    return tuple(item for item in evidence if item.candidate in candidates)


def _location_issues(
    selection: RuntimePersistenceSelection,
    evidence: tuple[LocationEvidence, ...],
) -> tuple[PersistenceIssue, ...]:
    selected = _selected_evidence(selection, evidence)
    issues = tuple(
        issue
        for item in selected
        for issue in item.candidate.assessment.issues
    )
    private_failures = tuple(
        item.private_auth
        for item in selected
        if isinstance(item.private_auth, PrivateAuthMigrationFailure)
    )
    if private_failures:
        issues = (
            *issues,
            PersistenceIssue(
                PersistenceCode.BACKUP_CONFLICT,
                None,
                private_failures[0].message,
            ),
        )
    elif (
        isinstance(selection, PartialSelection)
        and len(selected) == _AUTHORITATIVE_LOCATION_COUNT
    ):
        issues = (
            *issues,
            PersistenceIssue(
                PersistenceCode.BACKUP_CONFLICT,
                None,
                "Private-auth state differs between persistence locations.",
            ),
        )
    return issues


def _artifact_basename(
    selection: RuntimePersistenceSelection,
) -> str | None:
    if isinstance(selection, EmptySelection):
        return None
    if isinstance(selection, (ConflictSelection, PartialSelection)):
        for candidate in selection.candidates:
            if candidate.assessment.artifact_basename is not None:
                return candidate.assessment.artifact_basename
        return None
    return selection.candidate.assessment.artifact_basename


def _location_next_command(
    selection: RuntimePersistenceSelection,
) -> tuple[str, ...] | None:
    if isinstance(selection, CompatibilitySelection):
        return _MIGRATE_LOCATIONS_COMMAND
    if isinstance(selection, PrototypeSelection):
        return _MIGRATE_ACCOUNTS_COMMAND
    if isinstance(selection, CandidateBlockedSelection):
        return selection.candidate.assessment.next_command
    return None


def _failure_assessment(
    path: Path,
    error: ManagedFileReadError
    | UnsafeManagedFileError
    | UnsupportedFilesystemError,
) -> PersistenceAssessment:
    issue = PersistenceIssue(error.code, error.artifact_basename, str(error))
    return PersistenceAssessment(
        code=error.code,
        generation=StoredGeneration.UNKNOWN,
        schema_version=None,
        account_count=None,
        safe_path=path,
        artifact_basename=error.artifact_basename,
        write_blocked=True,
        next_command=(
            ("sidekick-usages", "permissions", "repair")
            if error.code is PersistenceCode.UNSAFE_PERMISSIONS
            else None
        ),
        message=str(error),
        issues=(issue,),
    )


__all__ = ["LocationEvidence", "LocationObserver"]

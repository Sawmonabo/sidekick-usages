"""Durable migration and reset coordinator behavior tests."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType

import pytest

from sidekick_usages.core.models import Account, ClaudeSetupTokenCredentials
from sidekick_usages.core.types import AccountLabel, ExitCode
from sidekick_usages.persistence._platform import FilesystemFamily
from sidekick_usages.persistence.artifacts import (
    ArtifactGrammar,
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileFingerprint,
    FileIdentity,
    FileSnapshot,
    ManagedArtifact,
    ManagedArtifactKind,
    Sha256Digest,
    sha256_digest,
)
from sidekick_usages.persistence.errors import (
    BackupConflictError,
    PersistenceCode,
    PrivateCredentialArtifactError,
    SourceChangedError,
    UnsafeManagedFileError,
)
from sidekick_usages.persistence.filesystem import (
    FilesystemQualification,
    PersistenceFilesystem,
)
from sidekick_usages.persistence.inventory import OrphanedPrivateCredentials
from sidekick_usages.persistence.migrations.account import (
    AccountMigrationCoordinator,
)
from sidekick_usages.persistence.migrations.errors import (
    PersistenceMigrationStateError,
    PrototypeReimportRequiredError,
    SchedulerMutationBlockedError,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialRepairResult,
)
from sidekick_usages.persistence.schemas import (
    PrototypeReceipt,
    decode_generation_zero,
    decode_prototype,
    decode_version_one,
    decode_version_two,
    encode_generation_zero,
    encode_prototype_receipt,
    encode_version_two,
)
from sidekick_usages.persistence.transforms import (
    accounts_to_version_two,
    prototype_to_version_two,
    version_two_to_v060,
)
from sidekick_usages.scheduler_quiescence import (
    SchedulerBackendId,
    SchedulerBackendObservation,
    SchedulerBackendState,
    SchedulerQuiescenceAssessment,
)
from tests.test_support import make_application_paths


def _snapshot(payload: bytes, *, inode: int) -> FileSnapshot:
    return FileSnapshot(
        FileFingerprint(
            FileIdentity(1, inode),
            sha256_digest(payload),
            len(payload),
        ),
        1,
        payload,
    )


class InMemoryFilesystem(PersistenceFilesystem):
    def __init__(self, path: Path, payload: bytes | None = None) -> None:
        self.authority_path = path
        self.grammar = ArtifactGrammar(path.name)
        self.snapshot = (
            _snapshot(payload, inode=1) if payload is not None else None
        )
        self.managed: dict[ManagedArtifact, FileSnapshot] = {}
        self.read_count = 0
        self._next_inode = 2

    def qualify(self) -> FilesystemQualification:
        return FilesystemQualification(
            FilesystemFamily.EXT4,
            self.authority_path,
        )

    def discover_managed(self) -> tuple[ManagedArtifact, ...]:
        return tuple(self.managed)

    def read_authority(self) -> FileSnapshot | None:
        self.read_count += 1
        return self.snapshot

    def read_external_private_source(self) -> FileSnapshot | None:
        self.read_count += 1
        return self.snapshot

    def read_managed(
        self,
        artifact: ManagedArtifact,
        *,
        limit: int = 16 * 1024 * 1024,
    ) -> FileSnapshot | None:
        del limit
        return self.managed.get(artifact)

    def replace_authority(self, payload: bytes) -> FileSnapshot:
        self.snapshot = _snapshot(payload, inode=self._next_inode)
        self._next_inode += 1
        return self.snapshot

    def seed_immutable(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
    ) -> ManagedArtifact:
        basename = self.grammar.backup_basename(
            generation,
            sha256_digest(payload),
        )
        artifact = self.grammar.parse(basename)
        assert artifact is not None
        self.managed[artifact] = _snapshot(
            payload,
            inode=self._next_inode,
        )
        self._next_inode += 1
        return artifact

    def seed_temporary(self, token: str) -> ManagedArtifact:
        basename = f".{self.authority_path.name}.authority.{token}.tmp"
        artifact = self.grammar.parse(basename)
        assert artifact is not None
        self.managed[artifact] = _snapshot(
            b"private candidate",
            inode=self._next_inode,
        )
        self._next_inode += 1
        return artifact


class InMemoryTransaction:
    def __init__(
        self,
        filesystem: InMemoryFilesystem,
        operation_log: list[str],
    ) -> None:
        self._filesystem = filesystem
        self._operation_log = operation_log

    def publish_immutable(
        self,
        generation: AuthorityGeneration,
        source: FileSnapshot,
    ) -> ManagedArtifact:
        basename = self._filesystem.grammar.backup_basename(
            generation,
            source.fingerprint.digest,
        )
        artifact = self._filesystem.grammar.parse(basename)
        assert artifact is not None
        existing = self._filesystem.managed.get(artifact)
        if existing is not None and existing.data != source.data:
            raise BackupConflictError(basename)
        if existing is None:
            self._filesystem.seed_immutable(generation, source.data)
        self._operation_log.append(f"snapshot:{generation}")
        return artifact

    def publish_migration_snapshot(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
    ) -> ManagedArtifact:
        artifact = self._filesystem.seed_immutable(generation, payload)
        self._operation_log.append(f"migration-snapshot:{generation}")
        return artifact

    def publish_receipt(
        self,
        prototype_digest: Sha256Digest,
        payload: bytes,
    ) -> ManagedArtifact:
        basename = self._filesystem.grammar.receipt_basename(prototype_digest)
        artifact = self._filesystem.grammar.parse(basename)
        assert artifact is not None
        existing = self._filesystem.managed.get(artifact)
        if existing is not None and existing.data != payload:
            raise BackupConflictError(basename)
        if existing is None:
            self._filesystem.managed[artifact] = _snapshot(
                payload,
                inode=900 + len(self._filesystem.managed),
            )
        self._operation_log.append("receipt")
        return artifact

    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        self._require_expected(expected_source)
        if generation is AuthorityGeneration.VERSION_ONE:
            decode_version_one(payload)
        elif generation is AuthorityGeneration.VERSION_TWO:
            decode_version_two(payload)
        else:
            decode_generation_zero(payload)
        committed = self._filesystem.replace_authority(payload)
        self._operation_log.append(f"commit:{generation}")
        return committed

    def recover_or_discard_temporary(
        self,
        temporary: ManagedArtifact,
    ) -> None:
        assert temporary.kind is ManagedArtifactKind.TEMPORARY
        self._filesystem.managed.pop(temporary, None)
        self._operation_log.append(f"recover:{temporary.basename}")

    def full_reset(self, expected_source: ExpectedAuthority) -> None:
        self._require_expected(expected_source)
        self._operation_log.append("reset")
        credential_kinds = {
            ManagedArtifactKind.GENERATION_ZERO_BACKUP,
            ManagedArtifactKind.VERSION_ONE_SNAPSHOT,
            ManagedArtifactKind.VERSION_TWO_SNAPSHOT,
            ManagedArtifactKind.TEMPORARY,
        }
        self._filesystem.managed = {
            artifact: snapshot
            for artifact, snapshot in self._filesystem.managed.items()
            if artifact.kind not in credential_kinds
        }
        self._filesystem.snapshot = None

    def _require_expected(self, expected: ExpectedAuthority) -> None:
        observed: ExpectedAuthority = (
            AuthorityExpectation.ABSENT
            if self._filesystem.snapshot is None
            else self._filesystem.snapshot.fingerprint
        )
        if observed != expected:
            raise SourceChangedError


class HeldInMemoryTransaction(AbstractContextManager[InMemoryTransaction]):
    def __init__(self, transaction: InMemoryTransaction) -> None:
        self._transaction = transaction

    def __enter__(self) -> InMemoryTransaction:
        return self._transaction

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        del exc_type, exc_value, traceback
        return False


class InMemoryLockFactory:
    def __init__(self, operation_log: list[str]) -> None:
        self._operation_log = operation_log

    def __call__(self, filesystem: PersistenceFilesystem) -> _InMemoryLock:
        if not isinstance(filesystem, InMemoryFilesystem):
            raise TypeError("In-memory lock requires its filesystem.")
        transaction = InMemoryTransaction(
            filesystem,
            self._operation_log,
        )
        return _InMemoryLock(transaction)


class _InMemoryLock:
    def __init__(self, transaction: InMemoryTransaction) -> None:
        self._transaction = transaction

    def hold(self) -> HeldInMemoryTransaction:
        return HeldInMemoryTransaction(self._transaction)


class InMemoryFilesystemFactory:
    def __init__(self, *filesystems: InMemoryFilesystem) -> None:
        self._filesystems = {
            filesystem.authority_path: filesystem for filesystem in filesystems
        }

    def __call__(self, path: Path) -> PersistenceFilesystem:
        return self._filesystems[path]


class SchedulerSequence:
    def __init__(
        self,
        *assessments: SchedulerQuiescenceAssessment,
    ) -> None:
        self._assessments = list(assessments)
        self.calls = 0

    def __call__(self) -> SchedulerQuiescenceAssessment:
        self.calls += 1
        if len(self._assessments) > 1:
            return self._assessments.pop(0)
        return self._assessments[0]


class RecordingVerifier:
    def __init__(self) -> None:
        self.preflight_calls = 0
        self.verified: list[FileSnapshot] = []
        self.preflight_error: Exception | None = None
        self.verify_error: Exception | None = None

    def preflight(self) -> None:
        self.preflight_calls += 1
        if self.preflight_error is not None:
            raise self.preflight_error

    def verify(self, account_path: Path, expected: FileSnapshot) -> None:
        assert account_path.is_absolute()
        if self.verify_error is not None:
            raise self.verify_error
        self.verified.append(expected)


class RecordingPrivateCredentials:
    def __init__(
        self,
        state: OrphanedPrivateCredentials,
        *,
        events: list[str] | None = None,
        fail_destroy: bool = False,
        remain_present: bool = False,
        fail_observe_at: int | None = None,
        after_destroy: Callable[[], None] | None = None,
    ) -> None:
        self.state = state
        self.events = events
        self.fail_destroy = fail_destroy
        self.remain_present = remain_present
        self.fail_observe_at = fail_observe_at
        self.after_destroy = after_destroy
        self.observe_calls = 0

    def observe(self) -> OrphanedPrivateCredentials:
        self.observe_calls += 1
        if self.events is not None:
            self.events.append("credentials:observe")
        if self.observe_calls == self.fail_observe_at:
            raise UnsafeManagedFileError("codex")
        return self.state

    def destroy_all(self) -> None:
        if self.events is not None:
            self.events.append("credentials:destroy")
        if self.fail_destroy:
            raise PrivateCredentialArtifactError
        if not self.remain_present:
            self.state = OrphanedPrivateCredentials.ABSENT
        if self.after_destroy is not None:
            self.after_destroy()

    def repair_permissions(
        self,
        *,
        locked_precondition: Callable[[], None],
    ) -> PrivateCredentialRepairResult:
        if self.events is not None:
            self.events.append("credentials:repair")
        locked_precondition()
        return PrivateCredentialRepairResult(
            root=Path.cwd() / "test-private-codex",
            account_parent_repaired=True,
            directories_repaired=1,
            files_repaired=1,
            artifacts_present=(
                self.state is OrphanedPrivateCredentials.PRESENT
            ),
        )


def _scheduler(state: SchedulerBackendState) -> SchedulerQuiescenceAssessment:
    return SchedulerQuiescenceAssessment(
        (
            SchedulerBackendObservation(
                SchedulerBackendId.SYSTEMD,
                state,
                "Sidekick scheduler test state.",
            ),
        )
    )


QUIET = _scheduler(SchedulerBackendState.ABSENT)
BLOCKED = _scheduler(SchedulerBackendState.INSTALLED)
EXPECTED_SCHEDULER_CHECKS = 2


def _account(
    label: str,
    *,
    targets: tuple[str, ...] | None = None,
) -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=ClaudeSetupTokenCredentials(
            access_token=f"test-only-{label}-access"
        ),
        plan="max",
        heartbeat_targets=targets,
    )


VERSION_TWO = encode_version_two(
    accounts_to_version_two((_account("claude-max-1"),))
)
GENERATION_ZERO = encode_generation_zero(
    version_two_to_v060(decode_version_two(VERSION_TWO))
)
EMPTY_VERSION_TWO = encode_version_two(accounts_to_version_two(()))
PROTOTYPE = b'{"imported":{"token":"test-only-prototype-access","plan":"max"}}'
PROTOTYPE_VERSION_TWO = encode_version_two(
    prototype_to_version_two(decode_prototype(PROTOTYPE))
)


def _service(
    root: Path,
    authority_payload: bytes | None,
    *,
    prototype_payload: bytes | None = None,
    scheduler: SchedulerSequence | None = None,
    verifier: RecordingVerifier | None = None,
    private_credentials: RecordingPrivateCredentials | None = None,
    operation_log: list[str] | None = None,
) -> tuple[
    AccountMigrationCoordinator,
    InMemoryFilesystem,
    InMemoryFilesystem,
    list[str],
    SchedulerSequence,
    RecordingVerifier,
]:
    paths = make_application_paths(root)
    authority = InMemoryFilesystem(
        paths.accounts.canonical,
        authority_payload,
    )
    prototype = InMemoryFilesystem(
        paths.accounts.prototype_cc_usage,
        prototype_payload,
    )
    operation_log = [] if operation_log is None else operation_log
    scheduler = scheduler or SchedulerSequence(QUIET)
    verifier = verifier or RecordingVerifier()
    private_credentials = private_credentials or RecordingPrivateCredentials(
        OrphanedPrivateCredentials.ABSENT
    )
    service = AccountMigrationCoordinator(
        paths.accounts.canonical,
        paths.accounts.prototype_cc_usage,
        scheduler_assessor=scheduler,
        private_credential_artifacts=private_credentials,
        released_v060_verifier=verifier,
        filesystem_factory=InMemoryFilesystemFactory(authority, prototype),
        lock_factory=InMemoryLockFactory(operation_log),
    )
    return (
        service,
        authority,
        prototype,
        operation_log,
        scheduler,
        verifier,
    )


def test_read_accounts_accepts_only_runtime_safe_snapshots(
    tmp_path: Path,
) -> None:
    """Doctor reads safe snapshots without constructing a mutable store."""
    current, *_ = _service(tmp_path / "current", VERSION_TWO)
    empty, *_ = _service(tmp_path / "empty", None)
    legacy, *_ = _service(tmp_path / "legacy", GENERATION_ZERO)

    assert [account.label for account in current.read_accounts()] == [
        "claude-max-1"
    ]
    assert empty.read_accounts() == ()
    with pytest.raises(PersistenceMigrationStateError):
        legacy.read_accounts()


def test_permission_repair_rechecks_scheduler_and_reassesses(
    tmp_path: Path,
) -> None:
    blocked_events: list[str] = []
    blocked_private = RecordingPrivateCredentials(
        OrphanedPrivateCredentials.PRESENT,
        events=blocked_events,
    )
    blocked_scheduler = SchedulerSequence(QUIET, BLOCKED)
    blocked, *_ = _service(
        tmp_path / "blocked",
        VERSION_TWO,
        scheduler=blocked_scheduler,
        private_credentials=blocked_private,
    )

    with pytest.raises(SchedulerMutationBlockedError):
        blocked.repair_permissions()

    assert blocked_scheduler.calls == EXPECTED_SCHEDULER_CHECKS
    assert blocked_events == ["credentials:repair"]

    repaired_events: list[str] = []
    repaired_private = RecordingPrivateCredentials(
        OrphanedPrivateCredentials.ABSENT,
        events=repaired_events,
    )
    repaired_scheduler = SchedulerSequence(QUIET, QUIET)
    repaired, *_ = _service(
        tmp_path / "repaired",
        VERSION_TWO,
        scheduler=repaired_scheduler,
        private_credentials=repaired_private,
    )

    result = repaired.repair_permissions()

    assert repaired_scheduler.calls == EXPECTED_SCHEDULER_CHECKS
    assert result.assessment.code is PersistenceCode.CURRENT
    assert result.repair.account_parent_repaired
    assert repaired_events == [
        "credentials:repair",
        "credentials:observe",
    ]


@pytest.mark.parametrize("checkpoint", ["source", "backup", "temporary"])
def test_generation_zero_migration_resumes_every_durable_checkpoint(
    tmp_path: Path,
    checkpoint: str,
) -> None:
    """Backup and safe temporary checkpoints converge on current v2."""
    service, authority, _prototype, log, scheduler, _verifier = _service(
        tmp_path,
        GENERATION_ZERO,
    )
    if checkpoint == "backup":
        authority.seed_immutable(
            AuthorityGeneration.GENERATION_ZERO,
            GENERATION_ZERO,
        )
    elif checkpoint == "temporary":
        authority.seed_temporary("0" * 32)

    result = service.migrate_accounts()

    assert result.code is PersistenceCode.CURRENT
    assert authority.snapshot is not None
    assert authority.snapshot.data == VERSION_TWO
    assert scheduler.calls == EXPECTED_SCHEDULER_CHECKS
    assert (
        sum(
            artifact.kind is ManagedArtifactKind.GENERATION_ZERO_BACKUP
            for artifact in authority.managed
        )
        == 1
    )
    assert not any(
        artifact.kind is ManagedArtifactKind.TEMPORARY
        for artifact in authority.managed
    )
    if checkpoint == "temporary":
        assert log[0].startswith("recover:")


def test_prototype_import_is_explicit_authority_first_and_resumable(
    tmp_path: Path,
) -> None:
    """Passive reads stay narrow while explicit import resumes receipts."""
    service, authority, _prototype, log, _scheduler, _verifier = _service(
        tmp_path / "initial",
        None,
        prototype_payload=PROTOTYPE,
    )
    result = service.migrate_accounts()
    assert result.code is PersistenceCode.PROTOTYPE_IMPORTED
    assert log.index("commit:v2") < log.index("receipt")
    assert authority.snapshot is not None
    assert authority.snapshot.data == PROTOTYPE_VERSION_TWO

    resumed, _authority, prototype, resume_log, _, _ = _service(
        tmp_path / "resume",
        PROTOTYPE_VERSION_TWO,
        prototype_payload=PROTOTYPE,
    )
    assert resumed.assess().code is PersistenceCode.CURRENT
    assert prototype.read_count == 0
    resumed_result = resumed.migrate_accounts()
    assert resumed_result.code is PersistenceCode.PROTOTYPE_IMPORTED
    assert resume_log == ["receipt"]

    reimport, reimport_authority, _, reimport_log, _, _ = _service(
        tmp_path / "reimport",
        VERSION_TWO,
        prototype_payload=PROTOTYPE,
    )
    with pytest.raises(PrototypeReimportRequiredError) as exc_info:
        reimport.migrate_accounts()
    assert exc_info.value.next_command[-1] == "--reimport-prototype"
    assert reimport_log == []
    assert (
        reimport.migrate_accounts(reimport_prototype=True).code
        is PersistenceCode.PROTOTYPE_IMPORTED
    )
    assert reimport_authority.snapshot is not None
    assert reimport_authority.snapshot.data == PROTOTYPE_VERSION_TWO
    assert reimport_log == ["snapshot:v2", "commit:v2", "receipt"]


def test_absent_authority_requires_reimport_for_historical_receipt(
    tmp_path: Path,
) -> None:
    """A historical import receipt cannot silently authorize replacement."""
    service, authority, _, log, _, _ = _service(
        tmp_path,
        None,
        prototype_payload=PROTOTYPE,
    )
    historical_digest = sha256_digest(b"historical prototype")
    receipt_name = authority.grammar.receipt_basename(historical_digest)
    receipt = authority.grammar.parse(receipt_name)
    assert receipt is not None
    authority.managed[receipt] = _snapshot(
        encode_prototype_receipt(PrototypeReceipt(historical_digest)),
        inode=77,
    )

    with pytest.raises(PrototypeReimportRequiredError):
        service.migrate_accounts()

    assert authority.snapshot is None
    assert log == []

    result = service.migrate_accounts(reimport_prototype=True)

    assert result.code is PersistenceCode.PROTOTYPE_IMPORTED
    assert authority.snapshot is not None
    assert authority.snapshot.data == PROTOTYPE_VERSION_TWO
    assert log == ["commit:v2", "receipt"]


def test_scheduler_state_and_ambiguous_persistence_block_without_mutation(
    tmp_path: Path,
) -> None:
    """The under-lock scheduler and full assessment are authoritative."""
    scheduler = SchedulerSequence(QUIET, BLOCKED)
    service, authority, _, log, _, _ = _service(
        tmp_path / "scheduler",
        GENERATION_ZERO,
        scheduler=scheduler,
    )
    before = authority.snapshot
    with pytest.raises(SchedulerMutationBlockedError) as scheduler_error:
        service.migrate_accounts()
    assert scheduler_error.value.exit_code is ExitCode.SCHEDULER_ERROR
    assert authority.snapshot == before
    assert log == []

    invalid, invalid_authority, _, invalid_log, _, _ = _service(
        tmp_path / "invalid",
        b"{",
    )
    with pytest.raises(PersistenceMigrationStateError) as invalid_error:
        invalid.migrate_accounts()
    assert invalid_error.value.code is PersistenceCode.MALFORMED_JSON
    assert invalid_authority.snapshot is not None
    assert invalid_authority.snapshot.data == b"{"
    assert invalid_log == []

    ambiguous, ambiguous_authority, _, ambiguous_log, _, _ = _service(
        tmp_path / "ambiguous",
        GENERATION_ZERO,
    )
    ambiguous_authority.seed_temporary("1" * 32)
    ambiguous_authority.seed_temporary("2" * 32)
    before_names = tuple(ambiguous_authority.managed)
    with pytest.raises(PersistenceMigrationStateError):
        ambiguous.migrate_accounts()
    assert tuple(ambiguous_authority.managed) == before_names
    assert ambiguous_log == []

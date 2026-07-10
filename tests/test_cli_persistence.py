"""Public CLI boundaries for explicit persistence operations."""

import io
import json
import os
from functools import partial
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages.cli import app
from sidekick_usages.cli import context as cli_context_module
from sidekick_usages.cli.context import (
    DaemonContext,
    DoctorBlocked,
    DoctorContext,
    InvocationContext,
    PersistenceContext,
    compose_app_context,
    compose_doctor_context,
)
from sidekick_usages.core.models import Account
from sidekick_usages.core.types import ExitCode
from sidekick_usages.daemon import (
    DaemonManager,
    DaemonOperation,
    DaemonOperationResult,
)
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
)
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceIssue,
    PersistenceOperationResult,
)
from sidekick_usages.persistence.errors import (
    PersistenceCode,
    PersistenceError,
    ResetIncompleteError,
    SourceChangedError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.migrations import (
    PermissionRepairOperationResult,
    PersistenceMigrationService,
)
from sidekick_usages.persistence.migrations.errors import (
    LocationMigrationStateError,
    PersistenceMigrationStateError,
    SchedulerMutationBlockedError,
)
from sidekick_usages.persistence.migrations.location import (
    BlockedLocationSelection,
    CandidateBlockedSelection,
    EmptySelection,
    LocationCandidate,
    LocationMigrationAssessment,
    LocationMigrationResult,
    LocationRole,
    RuntimePersistenceSelection,
)
from sidekick_usages.persistence.migrations.ports import (
    PrivateAuthMigrationAssessment,
)
from sidekick_usages.persistence.observations import StoredGeneration
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialRepairResult,
)
from sidekick_usages.persistence.schemas import (
    GenerationZeroDocument,
    VersionOneDocument,
    encode_generation_zero,
    encode_version_one,
)
from sidekick_usages.persistence.v060 import ReleasedV060Verifier
from sidekick_usages.providers.codex.auth_migration import (
    CodexPrivateAuthMigrator,
)
from sidekick_usages.scheduler_quiescence import (
    SchedulerBackendId,
    SchedulerBackendObservation,
    SchedulerBackendState,
    SchedulerQuiescenceAssessment,
)
from tests.test_support import CliHarness, make_application_paths

_SNAPSHOT_BASENAME = f"accounts.json.v1.{'a' * 64}.bak"
_PRIVATE_DIRECTORY_MODE = 0o700


def _assessment(
    root: Path,
    code: PersistenceCode,
    *,
    count: int | None,
    next_command: tuple[str, ...] | None = None,
) -> PersistenceAssessment:
    message = {
        PersistenceCode.EMPTY: "No account data is present.",
        PersistenceCode.CURRENT: "Account data uses the current schema.",
        PersistenceCode.FUTURE_SCHEMA: "Compatible software is required.",
        PersistenceCode.INVALID_SCHEMA: ("Account data violates its schema."),
        PersistenceCode.MALFORMED_JSON: "Account data is not strict JSON.",
        PersistenceCode.MIGRATION_REQUIRED: (
            "Account data requires migration."
        ),
        PersistenceCode.PROTOTYPE_IMPORT_REQUIRED: (
            "Prototype import is required."
        ),
        PersistenceCode.ROLLBACK_PREPARED: "A rollback snapshot matches.",
    }[code]
    generation = {
        PersistenceCode.EMPTY: StoredGeneration.ABSENT,
        PersistenceCode.PROTOTYPE_IMPORT_REQUIRED: StoredGeneration.ABSENT,
        PersistenceCode.FUTURE_SCHEMA: StoredGeneration.FUTURE,
        PersistenceCode.INVALID_SCHEMA: StoredGeneration.UNKNOWN,
        PersistenceCode.MALFORMED_JSON: StoredGeneration.UNKNOWN,
        PersistenceCode.MIGRATION_REQUIRED: StoredGeneration.GENERATION_ZERO,
        PersistenceCode.ROLLBACK_PREPARED: (StoredGeneration.GENERATION_ZERO),
    }.get(code, StoredGeneration.VERSION_ONE)
    issue = PersistenceIssue(code, None, message)
    return PersistenceAssessment(
        code=code,
        generation=generation,
        schema_version=(
            1 if generation is StoredGeneration.VERSION_ONE else None
        ),
        account_count=count,
        safe_path=root / "accounts.json",
        artifact_basename=None,
        write_blocked=code
        not in {PersistenceCode.EMPTY, PersistenceCode.CURRENT},
        next_command=next_command,
        message=message,
        issues=(issue,),
    )


class RecordingPersistence:
    """Record command ordering while returning closed persistence values."""

    def __init__(
        self,
        assessment: PersistenceAssessment,
        *,
        preview_error: SchedulerMutationBlockedError
        | PersistenceError
        | None = None,
        migration_error: PersistenceError | None = None,
        rollback_error: PersistenceError | None = None,
        repair_error: SchedulerMutationBlockedError
        | PersistenceError
        | None = None,
        reset_error: SchedulerMutationBlockedError
        | PersistenceError
        | None = None,
    ) -> None:
        self.assessment = assessment
        self.preview_error = preview_error
        self.migration_error = migration_error
        self.rollback_error = rollback_error
        self.repair_error = repair_error
        self.reset_error = reset_error
        self.events: list[str] = []

    def assess(self) -> PersistenceAssessment:
        self.events.append("assess")
        return self.assessment

    def assess_locations(
        self,
    ) -> LocationMigrationAssessment[RuntimePersistenceSelection]:
        path = self.assessment.safe_path
        return LocationMigrationAssessment(
            selection=EmptySelection(),
            candidates=(),
            source=path,
            destination=path,
            private_auth_summary=PrivateAuthMigrationAssessment(()),
            artifact_basename=None,
            issues=(),
            write_blocked=False,
            next_command=None,
        )

    def read_accounts(self) -> tuple[Account, ...]:
        return ()

    def mutation_preview(self) -> PersistenceAssessment:
        self.events.append("preview")
        if self.preview_error is not None:
            raise self.preview_error
        return self.assessment

    def location_migration_preview(
        self,
    ) -> LocationMigrationAssessment[RuntimePersistenceSelection]:
        self.events.append("location-preview")
        return self.assess_locations()

    def permission_repair_preview(self) -> PersistenceAssessment:
        return self.mutation_preview()

    def migrate_accounts(
        self,
        *,
        reimport_prototype: bool = False,
    ) -> PersistenceAssessment:
        self.events.append(f"migrate:{reimport_prototype}")
        if self.migration_error is not None:
            raise self.migration_error
        return self.assessment

    def migrate_locations(self) -> LocationMigrationResult:
        self.events.append("migrate-locations")
        raise LocationMigrationStateError(self.assess_locations())

    def prepare_rollback(self) -> PersistenceOperationResult:
        self.events.append("rollback")
        if self.rollback_error is not None:
            raise self.rollback_error
        return PersistenceOperationResult(
            PersistenceCode.ROLLBACK_PREPARED,
            self.assessment,
            _SNAPSHOT_BASENAME,
            "A rollback snapshot matches.",
        )

    def repair_permissions(self) -> PermissionRepairOperationResult:
        self.events.append("repair")
        if self.repair_error is not None:
            raise self.repair_error
        return PermissionRepairOperationResult(
            PrivateCredentialRepairResult(
                root=self.assessment.safe_path.parent / "codex",
                account_parent_repaired=True,
                directories_repaired=2,
                files_repaired=1,
                artifacts_present=True,
            ),
            self.assessment,
        )

    def full_reset(self) -> PersistenceAssessment:
        self.events.append("reset")
        if self.reset_error is not None:
            raise self.reset_error
        return self.assessment


def _install_context(
    root: Path,
    persistence: RecordingPersistence,
) -> tuple[CliHarness, io.StringIO, io.StringIO]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    harness = CliHarness(
        console=Console(file=stdout, width=200, force_terminal=False),
        err_console=Console(
            file=stderr,
            width=200,
            force_terminal=False,
        ),
        persistence=PersistenceContext(persistence),
    )
    return harness, stdout, stderr


def test_migrate_accounts_previews_before_confirmation_and_honors_intent(
    tmp_path: Path,
) -> None:
    assessment = _assessment(
        tmp_path,
        PersistenceCode.MIGRATION_REQUIRED,
        count=2,
        next_command=("sidekick-usages", "migrate", "accounts"),
    )
    cancelled = RecordingPersistence(assessment)
    cancelled_cli, cancelled_stdout, _ = _install_context(
        tmp_path / "cancel", cancelled
    )

    result = cancelled_cli.invoke(
        ["migrate", "accounts"],
        input_text="n\n",
    )

    assert result.exit_code == ExitCode.MANUAL_ACTION
    assert cancelled.events == ["preview"]
    assert "State: migration_required" in cancelled_stdout.getvalue()

    approved = RecordingPersistence(assessment)
    approved_cli, _, _ = _install_context(tmp_path / "approved", approved)
    result = approved_cli.invoke(
        ["migrate", "accounts", "--reimport-prototype", "--yes"],
    )
    assert result.exit_code == ExitCode.SUCCESS
    assert approved.events == ["preview", "migrate:True"]


def test_prepare_rollback_rejects_other_targets_and_reports_snapshot(
    tmp_path: Path,
) -> None:
    assessment = _assessment(
        tmp_path,
        PersistenceCode.CURRENT,
        count=1,
    )
    persistence = RecordingPersistence(assessment)
    harness, stdout, stderr = _install_context(tmp_path, persistence)

    rejected = harness.invoke(
        ["migrate", "prepare-rollback", "--target", "v0.5.0", "--yes"],
    )
    assert rejected.exit_code == ExitCode.MANUAL_ACTION
    assert persistence.events == []
    assert "Expected 'v0.6.0'" in stderr.getvalue()

    prepared = harness.invoke(
        ["migrate", "prepare-rollback", "--target", "v0.6.0", "--yes"],
    )
    assert prepared.exit_code == ExitCode.SUCCESS
    assert persistence.events == ["preview", "rollback"]
    assert _SNAPSHOT_BASENAME in stdout.getvalue()


def test_permissions_repair_requires_intent_and_maps_scheduler_block(
    tmp_path: Path,
) -> None:
    assessment = _assessment(tmp_path, PersistenceCode.CURRENT, count=1)
    cancelled = RecordingPersistence(assessment)
    cancelled_cli, cancelled_stdout, _ = _install_context(
        tmp_path / "cancel", cancelled
    )

    result = cancelled_cli.invoke(
        ["permissions", "repair"],
        input_text="n\n",
    )

    assert result.exit_code == ExitCode.MANUAL_ACTION
    assert cancelled.events == ["preview"]
    assert "Permission repair" in cancelled_stdout.getvalue()

    scheduler = SchedulerQuiescenceAssessment(
        (
            SchedulerBackendObservation(
                SchedulerBackendId.SYSTEMD,
                SchedulerBackendState.INSTALLED,
                "Sidekick scheduler is installed.",
            ),
        )
    )
    blocked = RecordingPersistence(
        assessment,
        preview_error=SchedulerMutationBlockedError(scheduler),
    )
    blocked_cli, _, _ = _install_context(tmp_path / "blocked", blocked)

    result = blocked_cli.invoke(["permissions", "repair", "--yes"])

    assert result.exit_code == ExitCode.SCHEDULER_ERROR
    assert blocked.events == ["preview"]

    approved = RecordingPersistence(assessment)
    approved_cli, approved_stdout, _ = _install_context(
        tmp_path / "approved", approved
    )

    result = approved_cli.invoke(["permissions", "repair", "--yes"])

    assert result.exit_code == ExitCode.SUCCESS
    assert approved.events == ["preview", "repair"]
    assert "private directories changed: 2" in approved_stdout.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="POSIX released-layout fixture")
def test_permissions_repair_restores_fresh_default_composition(
    tmp_path: Path,
) -> None:
    paths = make_application_paths(tmp_path)
    paths.accounts.canonical.parent.chmod(0o755)
    authority = encode_version_one(VersionOneDocument(()))
    paths.accounts.canonical.write_bytes(authority)
    paths.accounts.canonical.chmod(0o600)
    paths.private_codex.canonical.mkdir(mode=0o755)
    paths.private_codex.canonical.chmod(0o755)

    scheduler = SchedulerQuiescenceAssessment(
        (
            SchedulerBackendObservation(
                SchedulerBackendId.SYSTEMD,
                SchedulerBackendState.ABSENT,
                "Sidekick scheduler is absent.",
            ),
        )
    )
    persistence = PersistenceMigrationService(
        paths,
        scheduler_assessor=lambda: scheduler,
        private_auth_migrator=CodexPrivateAuthMigrator(),
        released_v060_verifier=ReleasedV060Verifier(),
    )
    harness = CliHarness(
        console=Console(file=io.StringIO(), force_terminal=False),
        err_console=Console(file=io.StringIO(), force_terminal=False),
        persistence=PersistenceContext(persistence),
    )
    result = harness.invoke(["permissions", "repair", "--yes"])

    assert result.exit_code == ExitCode.SUCCESS
    assert paths.accounts.canonical.read_bytes() == authority
    assert (
        paths.accounts.canonical.parent.stat().st_mode & 0o777
        == _PRIVATE_DIRECTORY_MODE
    )
    assert (
        paths.private_codex.canonical.stat().st_mode & 0o777
        == _PRIVATE_DIRECTORY_MODE
    )

    fresh = compose_app_context(
        paths=paths,
        providers={},
        heartbeat_providers={},
    )
    try:
        assert list(fresh.value.accounts) == []
    finally:
        fresh.close()


@pytest.mark.parametrize(
    ("error_kind", "expected_exit"),
    [
        ("scheduler", ExitCode.SCHEDULER_ERROR),
        ("passive", ExitCode.SYSTEM_ERROR),
        ("operation", ExitCode.MANUAL_ACTION),
    ],
)
def test_migration_errors_use_stable_exit_vocabulary(
    tmp_path: Path,
    error_kind: str,
    expected_exit: ExitCode,
) -> None:
    malformed = _assessment(
        tmp_path,
        PersistenceCode.MALFORMED_JSON,
        count=None,
    )
    if error_kind == "scheduler":
        scheduler = SchedulerQuiescenceAssessment(
            (
                SchedulerBackendObservation(
                    SchedulerBackendId.SYSTEMD,
                    SchedulerBackendState.INSTALLED,
                    "Sidekick scheduler is installed.",
                ),
            )
        )
        error: SchedulerMutationBlockedError | PersistenceError = (
            SchedulerMutationBlockedError(scheduler)
        )
    elif error_kind == "passive":
        error = PersistenceMigrationStateError(malformed)
    else:
        error = SourceChangedError()
    persistence = RecordingPersistence(malformed, preview_error=error)
    harness, _, stderr = _install_context(tmp_path, persistence)

    result = harness.invoke(["migrate", "accounts", "--yes"])

    assert result.exit_code == expected_exit
    assert persistence.events == ["preview"]
    assert stderr.getvalue()


@pytest.mark.parametrize(
    ("command", "expected_events"),
    [
        (
            ["migrate", "accounts", "--reimport-prototype", "--yes"],
            ["preview", "migrate:True"],
        ),
        (
            [
                "migrate",
                "prepare-rollback",
                "--target",
                "v0.6.0",
                "--yes",
            ],
            ["preview", "rollback"],
        ),
    ],
)
def test_rejected_transition_never_exits_success(
    tmp_path: Path,
    command: list[str],
    expected_events: list[str],
) -> None:
    assessment = _assessment(tmp_path, PersistenceCode.EMPTY, count=0)
    error = PersistenceMigrationStateError(assessment)
    is_rollback = "prepare-rollback" in command
    persistence = RecordingPersistence(
        assessment,
        migration_error=None if is_rollback else error,
        rollback_error=error if is_rollback else None,
    )
    harness, _, stderr = _install_context(tmp_path, persistence)

    result = harness.invoke(command)

    assert result.exit_code == ExitCode.MANUAL_ACTION
    assert persistence.events == expected_events
    assert "No account data is present." in stderr.getvalue()


def test_full_reset_executes_with_zero_validated_accounts(
    tmp_path: Path,
) -> None:
    persistence = RecordingPersistence(
        _assessment(tmp_path, PersistenceCode.EMPTY, count=0)
    )
    harness, stdout, _ = _install_context(tmp_path, persistence)

    result = harness.invoke(["reset", "--yes"])

    assert result.exit_code == ExitCode.SUCCESS
    assert persistence.events == ["preview", "reset"]
    assert "Cleared 0 account(s)" in stdout.getvalue()


def test_full_reset_maps_preview_and_mutation_failures(tmp_path: Path) -> None:
    assessment = _assessment(tmp_path, PersistenceCode.EMPTY, count=0)
    scheduler = SchedulerQuiescenceAssessment(
        (
            SchedulerBackendObservation(
                SchedulerBackendId.SYSTEMD,
                SchedulerBackendState.INSTALLED,
                "Sidekick scheduler is installed.",
            ),
        )
    )
    blocked = RecordingPersistence(
        assessment,
        preview_error=SchedulerMutationBlockedError(scheduler),
    )
    blocked_cli, _, _ = _install_context(tmp_path / "blocked", blocked)

    blocked_result = blocked_cli.invoke(["reset", "--yes"])

    assert blocked_result.exit_code == ExitCode.SCHEDULER_ERROR
    assert blocked.events == ["preview"]

    incomplete = RecordingPersistence(
        assessment,
        reset_error=ResetIncompleteError("accounts.json"),
    )
    incomplete_cli, _, _ = _install_context(
        tmp_path / "incomplete", incomplete
    )

    incomplete_result = incomplete_cli.invoke(["reset", "--yes"])

    assert incomplete_result.exit_code == ExitCode.SYSTEM_ERROR
    assert incomplete.events == ["preview", "reset"]


def test_doctor_renders_blocked_persistence_without_a_store(
    tmp_path: Path,
) -> None:
    persistence = RecordingPersistence(
        _assessment(
            tmp_path,
            PersistenceCode.MALFORMED_JSON,
            count=None,
        )
    )
    harness, stdout, _ = _install_context(tmp_path, persistence)
    candidate = LocationCandidate(
        role=LocationRole.COMPATIBILITY,
        path=persistence.assessment.safe_path,
        assessment=persistence.assessment,
        account_digest=None,
        private_auth_digest=None,
    )
    selection: BlockedLocationSelection = CandidateBlockedSelection(
        candidate,
        PersistenceCode.MALFORMED_JSON,
    )
    assessment: LocationMigrationAssessment[BlockedLocationSelection] = (
        LocationMigrationAssessment(
            selection=selection,
            candidates=(candidate,),
            source=candidate.path,
            destination=candidate.path,
            private_auth_summary=PrivateAuthMigrationAssessment(()),
            artifact_basename=None,
            issues=persistence.assessment.issues,
            write_blocked=True,
            next_command=None,
        )
    )
    harness.doctor = DoctorContext(DoctorBlocked(assessment))

    result = harness.invoke(["doctor", "--json"])

    assert result.exit_code == ExitCode.SYSTEM_ERROR
    payload = json.loads(stdout.getvalue())
    assert payload["accounts"] == []
    assert payload["persistence"]["code"] == "candidate_blocked"
    assert (
        payload["persistence"]["candidates"][0]["schema"]["code"]
        == "malformed_json"
    )
    stdout.seek(0)
    stdout.truncate()
    human = harness.invoke(["doctor"])
    assert human.exit_code == ExitCode.SYSTEM_ERROR
    assert "location: candidate_blocked" in stdout.getvalue()
    assert "state: malformed_json" in stdout.getvalue()
    assert persistence.events == []


def test_normal_command_reports_exact_migration_action(tmp_path: Path) -> None:
    paths = make_application_paths(tmp_path)
    filesystem = PersistenceFilesystem(paths.accounts.canonical)
    filesystem.repair_parent_permissions()
    with PersistenceLock(filesystem).hold() as transaction:
        transaction.commit_authority(
            AuthorityGeneration.GENERATION_ZERO,
            encode_generation_zero(GenerationZeroDocument(())),
            AuthorityExpectation.ABSENT,
        )
    stdout = io.StringIO()
    stderr = io.StringIO()
    invocation = InvocationContext(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        app_composer=partial(
            compose_app_context,
            paths=paths,
            providers={},
            heartbeat_providers={},
        ),
    )

    result = CliRunner().invoke(app, ["list"], obj=invocation)

    assert result.exit_code == ExitCode.MANUAL_ACTION
    assert "Next: sidekick-usages migrate accounts" in stderr.getvalue()


def test_scheduler_recovery_commands_do_not_require_account_store() -> None:
    stdout = io.StringIO()
    events: list[DaemonOperation] = []

    class FakeDaemonManager(DaemonManager):
        def __init__(self) -> None:
            pass

        def run(
            self,
            operation: str,
            backend: str = "auto",
        ) -> DaemonOperationResult:
            events.append(DaemonOperation(operation))
            return DaemonOperationResult(backend, "safe")

    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=io.StringIO(), force_terminal=False),
        daemon=DaemonContext(FakeDaemonManager()),
    )

    status = harness.invoke(["daemon", "status"])
    uninstall = harness.invoke(["daemon", "uninstall"])

    assert status.exit_code == ExitCode.SUCCESS
    assert uninstall.exit_code == ExitCode.SUCCESS
    assert events == [DaemonOperation.STATUS, DaemonOperation.UNINSTALL]
    assert "auto: safe" in stdout.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="POSIX legacy mode fixture")
def test_unsafe_private_root_composes_as_blocked_location_assessment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_application_paths(tmp_path)
    paths.private_codex.canonical.mkdir(mode=0o755)
    paths.private_codex.canonical.chmod(0o755)
    constructed_stores = 0

    def reject_store_construction(*_args: object, **_kwargs: object) -> None:
        nonlocal constructed_stores
        constructed_stores += 1
        raise AssertionError("blocked composition constructed AccountStore")

    monkeypatch.setattr(
        cli_context_module,
        "AccountStore",
        reject_store_construction,
    )

    owner = compose_doctor_context(
        paths=paths,
        providers={},
        heartbeat_providers={},
    )

    assert constructed_stores == 0
    state = owner.value.state
    assert isinstance(state, DoctorBlocked)
    selection = state.assessment.selection
    assert isinstance(selection, CandidateBlockedSelection)
    assert selection.persistence_code is PersistenceCode.UNSAFE_PERMISSIONS
    assert selection.candidate.path == paths.accounts.canonical

    stdout = io.StringIO()
    stderr = io.StringIO()
    harness = CliHarness(
        console=Console(file=stdout, width=80, force_terminal=False),
        err_console=Console(
            file=stderr,
            width=200,
            force_terminal=False,
        ),
        doctor=owner.value,
    )
    try:
        doctor = harness.invoke(["doctor", "--json"])
        payload = json.loads(stdout.getvalue())
        assert doctor.exit_code == ExitCode.SYSTEM_ERROR
        assert payload["persistence"]["code"] == "candidate_blocked"
        candidate = payload["persistence"]["candidates"][0]
        assert candidate["schema"]["code"] == "unsafe_permissions"
        assert candidate["path"] == str(paths.accounts.canonical)
    finally:
        owner.close()

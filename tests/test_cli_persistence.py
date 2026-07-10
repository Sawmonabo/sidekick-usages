"""Public CLI boundaries for explicit persistence operations."""

import io
import json
import os
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages import cli
from sidekick_usages.core.models import Account
from sidekick_usages.core.types import ExitCode
from sidekick_usages.daemon import DaemonOperation, DaemonOperationResult
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceIssue,
    PersistenceOperationResult,
    StoredGeneration,
)
from sidekick_usages.persistence.errors import (
    PersistenceCode,
    PersistenceError,
    ResetIncompleteError,
    SourceChangedError,
)
from sidekick_usages.persistence.migration_errors import (
    PersistenceMigrationStateError,
    SchedulerMutationBlockedError,
)
from sidekick_usages.persistence.migrations import (
    PermissionRepairOperationResult,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialRepairResult,
)
from sidekick_usages.persistence.schemas import (
    VersionOneDocument,
    encode_version_one,
)
from sidekick_usages.scheduler_quiescence import (
    SchedulerBackendId,
    SchedulerBackendObservation,
    SchedulerBackendState,
    SchedulerQuiescenceAssessment,
)
from tests.test_support import FixedClock, make_application_paths

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

    def read_accounts(self) -> tuple[Account, ...]:
        return ()

    def mutation_preview(self) -> PersistenceAssessment:
        self.events.append("preview")
        if self.preview_error is not None:
            raise self.preview_error
        return self.assessment

    def migrate_accounts(
        self,
        *,
        reimport_prototype: bool = False,
    ) -> PersistenceAssessment:
        self.events.append(f"migrate:{reimport_prototype}")
        if self.migration_error is not None:
            raise self.migration_error
        return self.assessment

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
) -> tuple[io.StringIO, io.StringIO]:
    paths = make_application_paths(root)
    stdout = io.StringIO()
    stderr = io.StringIO()
    cli.set_context(
        cli.AppContext(
            store=None,
            http=HttpClient(),
            providers={},
            heartbeat_providers={},
            private_codex_locations=paths.private_codex,
            lifetime_sources={},
            console=Console(file=stdout, width=200, force_terminal=False),
            err_console=Console(
                file=stderr,
                width=200,
                force_terminal=False,
            ),
            clock=FixedClock(),
            persistence=persistence,
            persistence_assessment=persistence.assessment,
        )
    )
    return stdout, stderr


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
    cancelled_stdout, _ = _install_context(tmp_path / "cancel", cancelled)

    result = CliRunner().invoke(
        cli.app,
        ["migrate", "accounts"],
        input="n\n",
    )

    assert result.exit_code == ExitCode.MANUAL_ACTION
    assert cancelled.events == ["preview"]
    assert "State: migration_required" in cancelled_stdout.getvalue()

    approved = RecordingPersistence(assessment)
    _install_context(tmp_path / "approved", approved)
    result = CliRunner().invoke(
        cli.app,
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
    stdout, stderr = _install_context(tmp_path, persistence)

    rejected = CliRunner().invoke(
        cli.app,
        ["migrate", "prepare-rollback", "--target", "v0.5.0", "--yes"],
    )
    assert rejected.exit_code == ExitCode.MANUAL_ACTION
    assert persistence.events == []
    assert "Expected 'v0.6.0'" in stderr.getvalue()

    prepared = CliRunner().invoke(
        cli.app,
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
    cancelled_stdout, _ = _install_context(tmp_path / "cancel", cancelled)

    result = CliRunner().invoke(
        cli.app,
        ["permissions", "repair"],
        input="n\n",
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
    _install_context(tmp_path / "blocked", blocked)

    result = CliRunner().invoke(
        cli.app,
        ["permissions", "repair", "--yes"],
    )

    assert result.exit_code == ExitCode.SCHEDULER_ERROR
    assert blocked.events == ["preview"]

    approved = RecordingPersistence(assessment)
    approved_stdout, _ = _install_context(tmp_path / "approved", approved)

    result = CliRunner().invoke(
        cli.app,
        ["permissions", "repair", "--yes"],
    )

    assert result.exit_code == ExitCode.SUCCESS
    assert approved.events == ["preview", "repair"]
    assert "private directories changed: 2" in approved_stdout.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="POSIX released-layout fixture")
def test_permissions_repair_restores_fresh_default_composition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_application_paths(tmp_path)
    paths.accounts.canonical.parent.chmod(0o755)
    authority = encode_version_one(VersionOneDocument(()))
    paths.accounts.canonical.write_bytes(authority)
    paths.accounts.canonical.chmod(0o600)
    paths.private_codex.canonical.mkdir(mode=0o755)
    paths.private_codex.canonical.chmod(0o755)

    class QuietDaemonManager:
        def assess_quiescence(self) -> SchedulerQuiescenceAssessment:
            return SchedulerQuiescenceAssessment(
                (
                    SchedulerBackendObservation(
                        SchedulerBackendId.SYSTEMD,
                        SchedulerBackendState.ABSENT,
                        "Sidekick scheduler is absent.",
                    ),
                )
            )

    monkeypatch.setattr(cli, "discover_application_paths", lambda: paths)
    monkeypatch.setattr(cli, "DaemonManager", QuietDaemonManager)
    cli._ContextState.ctx = None

    result = CliRunner().invoke(
        cli.app,
        ["permissions", "repair", "--yes"],
    )

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

    cli._ContextState.ctx = None
    fresh = cli._build_default_context()
    try:
        assert fresh.persistence_failure is None
        assert fresh.persistence_assessment is not None
        assert fresh.persistence_assessment.code is PersistenceCode.CURRENT
        assert list(fresh.require_store()) == []
    finally:
        fresh.http.close()


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
    _, stderr = _install_context(tmp_path, persistence)

    result = CliRunner().invoke(
        cli.app,
        ["migrate", "accounts", "--yes"],
    )

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
    _, stderr = _install_context(tmp_path, persistence)

    result = CliRunner().invoke(cli.app, command)

    assert result.exit_code == ExitCode.MANUAL_ACTION
    assert persistence.events == expected_events
    assert "No account data is present." in stderr.getvalue()


def test_full_reset_executes_with_zero_validated_accounts(
    tmp_path: Path,
) -> None:
    persistence = RecordingPersistence(
        _assessment(tmp_path, PersistenceCode.EMPTY, count=0)
    )
    stdout, _ = _install_context(tmp_path, persistence)

    result = CliRunner().invoke(cli.app, ["reset", "--yes"])

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
    _install_context(tmp_path / "blocked", blocked)

    blocked_result = CliRunner().invoke(cli.app, ["reset", "--yes"])

    assert blocked_result.exit_code == ExitCode.SCHEDULER_ERROR
    assert blocked.events == ["preview"]

    incomplete = RecordingPersistence(
        assessment,
        reset_error=ResetIncompleteError("accounts.json"),
    )
    _install_context(tmp_path / "incomplete", incomplete)

    incomplete_result = CliRunner().invoke(cli.app, ["reset", "--yes"])

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
    stdout, _ = _install_context(tmp_path, persistence)

    result = CliRunner().invoke(cli.app, ["doctor", "--json"])

    assert result.exit_code == ExitCode.SYSTEM_ERROR
    assert '"code": "malformed_json"' in stdout.getvalue()
    assert '"accounts": []' in stdout.getvalue()
    assert persistence.events == []


def test_normal_command_reports_exact_migration_action(tmp_path: Path) -> None:
    assessment = _assessment(
        tmp_path,
        PersistenceCode.MIGRATION_REQUIRED,
        count=1,
        next_command=("sidekick-usages", "migrate", "accounts"),
    )
    persistence = RecordingPersistence(assessment)
    _, stderr = _install_context(tmp_path, persistence)

    result = CliRunner().invoke(cli.app, ["list"])

    assert result.exit_code == ExitCode.MANUAL_ACTION
    assert "Next: sidekick-usages migrate accounts" in stderr.getvalue()
    assert persistence.events == []


@pytest.mark.parametrize(
    ("code", "next_command", "expected_exit"),
    [
        (
            PersistenceCode.MIGRATION_REQUIRED,
            ("sidekick-usages", "migrate", "accounts"),
            ExitCode.MANUAL_ACTION,
        ),
        (
            PersistenceCode.PROTOTYPE_IMPORT_REQUIRED,
            ("sidekick-usages", "migrate", "accounts"),
            ExitCode.MANUAL_ACTION,
        ),
        (PersistenceCode.FUTURE_SCHEMA, None, ExitCode.MANUAL_ACTION),
        (PersistenceCode.INVALID_SCHEMA, None, ExitCode.SYSTEM_ERROR),
    ],
)
def test_python_module_entrypoint_fails_closed_without_traceback(
    tmp_path: Path,
    code: PersistenceCode,
    next_command: tuple[str, ...] | None,
    expected_exit: ExitCode,
) -> None:
    assessment = _assessment(
        tmp_path,
        code,
        count=None,
        next_command=next_command,
    )
    persistence = RecordingPersistence(assessment)
    _, stderr = _install_context(tmp_path, persistence)
    exit_code = cli._run_typer(["list"])

    rendered = stderr.getvalue()
    assert exit_code == expected_exit
    assert assessment.message in rendered
    assert "Traceback" not in rendered
    if next_command is not None:
        assert "Next: sidekick-usages migrate accounts" in rendered


def test_scheduler_recovery_commands_do_not_require_account_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persistence = RecordingPersistence(
        _assessment(
            tmp_path,
            PersistenceCode.MIGRATION_REQUIRED,
            count=1,
        )
    )
    stdout, _ = _install_context(tmp_path, persistence)
    events: list[DaemonOperation] = []

    class FakeDaemonManager:
        def run(
            self,
            operation: DaemonOperation,
            backend: str,
        ) -> DaemonOperationResult:
            events.append(operation)
            return DaemonOperationResult(backend, "safe")

    monkeypatch.setattr(cli, "DaemonManager", FakeDaemonManager)

    status = CliRunner().invoke(cli.app, ["daemon", "status"])
    uninstall = CliRunner().invoke(cli.app, ["daemon", "uninstall"])

    assert status.exit_code == ExitCode.SUCCESS
    assert uninstall.exit_code == ExitCode.SUCCESS
    assert events == [DaemonOperation.STATUS, DaemonOperation.UNINSTALL]
    assert "auto: safe" in stdout.getvalue()


@pytest.mark.skipif(os.name == "nt", reason="POSIX legacy mode fixture")
def test_unsafe_private_root_composes_as_passive_doctor_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = make_application_paths(tmp_path)
    paths.private_codex.canonical.mkdir(mode=0o755)
    constructed_stores = 0

    def reject_store_construction(*_args: object, **_kwargs: object) -> None:
        nonlocal constructed_stores
        constructed_stores += 1
        raise AssertionError("blocked composition constructed AccountStore")

    class QuietDaemonManager:
        def assess_quiescence(self) -> SchedulerQuiescenceAssessment:
            return SchedulerQuiescenceAssessment(
                (
                    SchedulerBackendObservation(
                        SchedulerBackendId.SYSTEMD,
                        SchedulerBackendState.ABSENT,
                        "Sidekick scheduler is absent.",
                    ),
                )
            )

    monkeypatch.setattr(cli, "discover_application_paths", lambda: paths)
    monkeypatch.setattr(cli, "DaemonManager", QuietDaemonManager)
    monkeypatch.setattr(cli, "AccountStore", reject_store_construction)

    context = cli._build_default_context()

    assert constructed_stores == 0
    assert context.store is None
    assert context.persistence_assessment is None
    failure = context.persistence_failure
    assert failure is not None
    assert failure.code is PersistenceCode.UNSAFE_PERMISSIONS
    assert failure.safe_path == paths.private_codex.canonical
    assert failure.artifact_basename == paths.private_codex.canonical.name

    stdout = io.StringIO()
    stderr = io.StringIO()
    context.console = Console(file=stdout, width=80, force_terminal=False)
    context.err_console = Console(
        file=stderr,
        width=200,
        force_terminal=False,
    )
    cli.set_context(context)

    doctor = CliRunner().invoke(cli.app, ["doctor", "--json"])
    payload = json.loads(stdout.getvalue())
    assert doctor.exit_code == ExitCode.SYSTEM_ERROR
    assert payload["persistence"]["code"] == "unsafe_permissions"
    assert payload["persistence"]["safe_path"] == str(
        paths.private_codex.canonical
    )
    stdout.seek(0)
    stdout.truncate()

    migrate = CliRunner().invoke(
        cli.app,
        ["migrate", "accounts", "--yes"],
    )
    reset = CliRunner().invoke(cli.app, ["reset", "--yes"])
    listed = CliRunner().invoke(cli.app, ["list"])
    assert (
        migrate.exit_code,
        reset.exit_code,
        listed.exit_code,
    ) == (
        ExitCode.SYSTEM_ERROR,
        ExitCode.SYSTEM_ERROR,
        ExitCode.SYSTEM_ERROR,
    )
    assert "Traceback" not in stderr.getvalue()

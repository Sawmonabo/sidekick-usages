"""Credential-refresh migration, reset, doctor, and daemon lifecycle tests."""

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread

import pytest

from sidekick_usages.cli.context import DoctorReady, compose_doctor_context
from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.models import (
    CredentialRefreshResult,
    CredentialRefreshSuccess,
)
from sidekick_usages.credentials.refresh import (
    CredentialRefreshCoordinator,
    CredentialRefreshReason,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.maintenance import TokenMaintenanceService
from sidekick_usages.persistence.credential_refresh import (
    CredentialRefreshArtifacts,
    CredentialRefreshCrashPoint,
    CredentialRefreshRecoveryBlockedError,
    CredentialRefreshStateKind,
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.migrations.service import (
    PersistenceMigrationService,
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
from tests.credential_refresh_support import (
    BlockingRefreshProvider,
    CrashAt,
    RefreshProvider,
    SimulatedCrashError,
    login_account,
)
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_account_store,
    make_application_paths,
)

_QUIET = SchedulerQuiescenceAssessment(
    (
        SchedulerBackendObservation(
            SchedulerBackendId.SYSTEMD,
            SchedulerBackendState.ABSENT,
            "Sidekick scheduler is absent.",
        ),
    )
)


def _service(root: Path) -> PersistenceMigrationService:
    """Compose real lifecycle coordination below one isolated root."""
    return PersistenceMigrationService(
        make_application_paths(root),
        scheduler_assessor=lambda: _QUIET,
        private_auth_migrator=CodexPrivateAuthMigrator(),
        released_v060_verifier=ReleasedV060Verifier(),
    )


@dataclass(slots=True)
class _ReasonRecorder:
    """Record application reasons without crossing a provider boundary."""

    calls: list[tuple[AccountLabel, CredentialRefreshReason]]

    def refresh(
        self,
        *,
        label: AccountLabel,
        reason: CredentialRefreshReason,
    ) -> CredentialRefreshResult:
        self.calls.append((label, reason))
        return CredentialRefreshSuccess(label)


def test_lifecycle_exclusion_waits_for_inflight_refresh(
    tmp_path: Path,
) -> None:
    """Migration/reset exclusion cannot overlap provider refresh I/O."""
    store = make_account_store(tmp_path, (login_account(),))
    entered = Event()
    release = Event()
    provider = BlockingRefreshProvider(entered, release)
    root = tmp_path / "credential-refresh"
    coordinator = CredentialRefreshCoordinator(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        CredentialRefreshTransactions(store, root),
        clock=FixedClock(),
    )
    refresh_failure: list[BaseException] = []

    def refresh() -> None:
        try:
            coordinator.refresh(
                label=AccountLabel("claude-team"),
                reason=CredentialRefreshReason.OPERATOR_FORCED,
            )
        except BaseException as error:
            refresh_failure.append(error)

    refresh_thread = Thread(target=refresh)
    refresh_thread.start()
    assert entered.wait(timeout=5)
    lifecycle_acquired = Event()
    lifecycle_failure: list[BaseException] = []

    def hold_lifecycle() -> None:
        try:
            with CredentialRefreshArtifacts(root).hold_quiescent():
                lifecycle_acquired.set()
        except BaseException as error:
            lifecycle_failure.append(error)

    lifecycle_thread = Thread(target=hold_lifecycle)
    lifecycle_thread.start()
    assert not lifecycle_acquired.wait(timeout=0.05)
    release.set()
    refresh_thread.join(timeout=5)
    lifecycle_thread.join(timeout=5)

    assert refresh_failure == []
    assert lifecycle_failure == []
    assert lifecycle_acquired.is_set()


def test_maintenance_routes_scheduled_and_forced_reasons(
    tmp_path: Path,
) -> None:
    """Maintenance cannot call a reasonless raw rotating refresh."""
    account = login_account(
        access_expiry=KnownExpiry(REFERENCE_TIME - timedelta(seconds=1))
    )
    recorder = _ReasonRecorder([])
    maintenance = TokenMaintenanceService(
        make_account_store(tmp_path, (account,)),
        recorder,
        clock=FixedClock(),
    )

    scheduled = maintenance.refresh_account(account)
    forced = maintenance.refresh_account(account, force=True)

    assert scheduled.refreshed is True
    assert forced.refreshed is True
    assert recorder.calls == [
        (account.label, CredentialRefreshReason.SCHEDULED_DUE),
        (account.label, CredentialRefreshReason.OPERATOR_FORCED),
    ]


def test_full_reset_removes_refresh_secrets_before_final_authority(
    tmp_path: Path,
) -> None:
    """Full reset proves refresh evidence and account authority absent."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    paths = make_application_paths(tmp_path)
    refresh_root = paths.credential_refresh
    coordinator = CredentialRefreshCoordinator(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: RefreshProvider()},
        CredentialRefreshTransactions(
            store,
            refresh_root,
            faults=CrashAt(CredentialRefreshCrashPoint.STAGE_COMPLETE),
        ),
        clock=FixedClock(),
    )
    with pytest.raises(SimulatedCrashError):
        coordinator.refresh(
            label=label,
            reason=CredentialRefreshReason.OPERATOR_FORCED,
        )
    stage = next(refresh_root.glob("*/replacement.json"))
    evidence = stage.parent
    assert (evidence / "intent.json").is_file()

    result = _service(tmp_path).full_reset()

    assert result.account_count == 0
    assert not store.path.exists()
    assert not evidence.exists()
    assert not any(path.is_dir() for path in refresh_root.iterdir())


def test_account_migration_resolves_completed_refresh_stage_first(
    tmp_path: Path,
) -> None:
    """Schema mutation consumes local stage without another exchange."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    provider = RefreshProvider()
    root = make_application_paths(tmp_path).credential_refresh
    coordinator = CredentialRefreshCoordinator(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        CredentialRefreshTransactions(
            store,
            root,
            faults=CrashAt(CredentialRefreshCrashPoint.STAGE_COMPLETE),
        ),
        clock=FixedClock(),
    )
    with pytest.raises(SimulatedCrashError):
        coordinator.refresh(
            label=label,
            reason=CredentialRefreshReason.OPERATOR_FORCED,
        )

    _service(tmp_path).migrate_accounts()

    assert len(provider.calls) == 1
    reopened = make_account_store(tmp_path).get(str(label))
    assert reopened is not None
    assert reopened.access_token == "sk-ant-oat01-new"
    assert not any(path.is_dir() for path in root.iterdir())


def test_unsafe_refresh_evidence_blocks_account_migration(
    tmp_path: Path,
) -> None:
    """Migration cannot ignore malformed private refresh state."""
    make_account_store(tmp_path, (login_account(),))
    root = make_application_paths(tmp_path).credential_refresh
    root.mkdir(mode=0o700)
    evidence = root / ("c" * 64)
    evidence.mkdir(mode=0o700)
    journal = evidence / "intent.json"
    journal.write_bytes(b"{")
    journal.chmod(0o600)

    with pytest.raises(CredentialRefreshRecoveryBlockedError):
        _service(tmp_path).migrate_accounts()

    assert journal.exists()


def test_doctor_reports_blocked_refresh_without_path_or_digest(
    tmp_path: Path,
) -> None:
    """Doctor exposes a closed refresh state without routing metadata."""
    make_account_store(tmp_path, (login_account(),))
    paths = make_application_paths(tmp_path)
    root = paths.credential_refresh
    root.mkdir(mode=0o700)
    evidence = root / ("d" * 64)
    evidence.mkdir(mode=0o700)
    journal = evidence / "intent.json"
    journal.write_bytes(b"{")
    journal.chmod(0o600)

    composed = compose_doctor_context(paths=paths, clock=FixedClock())
    state = composed.value.state
    composed.close()

    assert isinstance(state, DoctorReady)
    assert state.refresh_state.kind is CredentialRefreshStateKind.BLOCKED
    assert "d" * 64 not in repr(state.refresh_state)
    assert str(root) not in repr(state.refresh_state)


def test_doctor_blocks_unexpected_refresh_root_file(
    tmp_path: Path,
) -> None:
    """Only recognized lock sidecars may be direct refresh-root files."""
    make_account_store(tmp_path, (login_account(),))
    root = make_application_paths(tmp_path).credential_refresh
    root.mkdir(mode=0o700)
    unexpected = root / "unexpected-state.json"
    unexpected.write_bytes(b"test-only-unexpected-state")
    unexpected.chmod(0o600)

    state = CredentialRefreshArtifacts(root).assess()

    assert state.kind is CredentialRefreshStateKind.BLOCKED


def test_full_reset_blocks_before_authority_when_refresh_namespace_is_unknown(
    tmp_path: Path,
) -> None:
    """Reset cannot report clean while unexpected refresh files remain."""
    store = make_account_store(tmp_path, (login_account(),))
    root = make_application_paths(tmp_path).credential_refresh
    root.mkdir(mode=0o700)
    unexpected = root / "unexpected-state.json"
    unexpected.write_bytes(b"test-only-unexpected-state")
    unexpected.chmod(0o600)

    with pytest.raises(CredentialRefreshRecoveryBlockedError):
        _service(tmp_path).full_reset()

    assert store.path.exists()
    assert unexpected.exists()

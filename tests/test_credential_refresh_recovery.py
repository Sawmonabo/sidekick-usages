"""Crash recovery and fail-closed credential-refresh evidence tests."""

import os
import subprocess
import sys
from pathlib import Path
from threading import Event, Thread
from typing import Never

import pytest

from sidekick_usages.cli.contexts.composition import compose_app_context
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.refresh import (
    CredentialRefreshCoordinator,
    CredentialRefreshReason,
)
from sidekick_usages.http.client import HttpClient
from sidekick_usages.persistence.credentials.refresh.service import (
    CredentialRefreshCrashPoint,
    CredentialRefreshRecoveryBlockedError,
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.errors import DurabilityUncertainError
from sidekick_usages.persistence.private.bundles.writes import (
    MAX_PRIVATE_FILE_BYTES,
)
from tests.fakes.credential_refresh import (
    CrashAt,
    RefreshProvider,
    SimulatedCrashError,
    login_account,
)
from tests.support.accounts import RuntimeCredentialResolver
from tests.support.persistence import (
    make_account_store,
    make_application_paths,
    remove_saved_account,
)
from tests.support.time import REFERENCE_TIME, FixedClock


@pytest.mark.parametrize(
    ("crash_point", "access_at_crash", "access_after_recovery"),
    [
        (CredentialRefreshCrashPoint.INTENT_WRITTEN, "old", "old"),
        (CredentialRefreshCrashPoint.STAGE_WRITTEN, "old", "new"),
        (CredentialRefreshCrashPoint.STAGE_COMPLETE, "old", "new"),
        (CredentialRefreshCrashPoint.ACCOUNT_COMMITTED, "new", "new"),
        (CredentialRefreshCrashPoint.JOURNAL_COMMITTED, "new", "new"),
        (CredentialRefreshCrashPoint.CLEANED, "new", "new"),
    ],
)
def test_every_crash_point_recovers_without_another_provider_call(
    tmp_path: Path,
    crash_point: CredentialRefreshCrashPoint,
    access_at_crash: str,
    access_after_recovery: str,
) -> None:
    """Every durable point converges locally without another exchange."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    provider = RefreshProvider()
    root = tmp_path / "credential-refresh"
    interrupted = CredentialRefreshTransactions(
        store,
        root,
        faults=CrashAt(crash_point),
    )
    coordinator = CredentialRefreshCoordinator(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        interrupted,
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(store),
    )

    with pytest.raises(SimulatedCrashError):
        coordinator.refresh(
            provider_id=ProviderId.CLAUDE,
            label=label,
            reason=CredentialRefreshReason.OPERATOR_FORCED,
        )

    calls_at_crash = len(provider.calls)
    crashed = store.get(str(label))
    assert crashed is not None
    assert crashed.access_token == f"sk-ant-oat01-{access_at_crash}"
    for journal_path in root.glob("*/intent.json"):
        journal = journal_path.read_text()
        assert str(label) not in journal
        assert "refresh-old" not in journal
        assert "sk-ant-oat01-old" not in journal
        assert "refresh-new" not in journal
        assert "sk-ant-oat01-new" not in journal

    CredentialRefreshTransactions(store, root).recover()
    assert len(provider.calls) == calls_at_crash
    saved = store.get(str(label))
    assert saved is not None
    assert saved.access_token == f"sk-ant-oat01-{access_after_recovery}"
    assert saved.refresh_token == f"refresh-{access_after_recovery}"
    assert not any(path.is_dir() for path in root.iterdir())


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux provider-stage mode normalization",
)
def test_interrupted_nonwritable_provider_stage_recovers_locally(
    tmp_path: Path,
) -> None:
    """Recovery hardens and removes a safe child left by the provider CLI."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    root = tmp_path / "credential-refresh"
    interrupted = CredentialRefreshTransactions(store, root)
    with interrupted.hold_stable(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.CREDENTIAL_REQUIRED.value,
        started_at=REFERENCE_TIME,
    ) as lease:
        stage_home = interrupted.prepare_provider_stage(lease)
        backups = stage_home / ".claude" / "backups"
        backups.mkdir(mode=0o755)
        backups.chmod(0o755)

    CredentialRefreshTransactions(store, root).recover()

    saved = store.get(str(label))
    assert saved is not None
    assert saved.access_token == "sk-ant-oat01-old"
    assert not any(path.is_dir() for path in root.iterdir())


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux provider-stage mode normalization",
)
def test_application_startup_recovers_safe_provider_stage(
    tmp_path: Path,
) -> None:
    """Normal application composition resolves local evidence before use."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    paths = make_application_paths(tmp_path)
    interrupted = CredentialRefreshTransactions(
        store,
        paths.credential_refresh,
    )
    with interrupted.hold_stable(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.CREDENTIAL_REQUIRED.value,
        started_at=REFERENCE_TIME,
    ) as lease:
        stage_home = interrupted.prepare_provider_stage(lease)
        backups = stage_home / ".claude" / "backups"
        backups.mkdir(mode=0o755)
        backups.chmod(0o755)

    composed = compose_app_context(
        paths=paths,
        providers={},
        heartbeat_providers={},
        local_activity_sources={},
        account_activity_sources={},
    )
    composed.close()

    saved = store.read_fresh(label)
    assert saved is not None
    assert saved.access_token == "sk-ant-oat01-old"
    assert not any(
        path.is_dir() for path in paths.credential_refresh.iterdir()
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_interrupted_writable_provider_stage_remains_blocked(
    tmp_path: Path,
) -> None:
    """Recovery never hardens a provider directory another user could alter."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    root = tmp_path / "credential-refresh"
    interrupted = CredentialRefreshTransactions(store, root)
    with interrupted.hold_stable(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.CREDENTIAL_REQUIRED.value,
        started_at=REFERENCE_TIME,
    ) as lease:
        stage_home = interrupted.prepare_provider_stage(lease)
        backups = stage_home / ".claude" / "backups"
        backups.mkdir(mode=0o777)
        backups.chmod(0o777)

    with pytest.raises(CredentialRefreshRecoveryBlockedError):
        CredentialRefreshTransactions(store, root).recover()

    assert backups.is_dir()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACL boundary")
def test_macos_acl_free_provider_stage_recovers(tmp_path: Path) -> None:
    """An exact-mode stage without an extended ACL remains recoverable."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    root = tmp_path / "credential-refresh"
    interrupted = CredentialRefreshTransactions(store, root)
    with interrupted.hold_stable(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.CREDENTIAL_REQUIRED.value,
        started_at=REFERENCE_TIME,
    ) as lease:
        stage_home = interrupted.prepare_provider_stage(lease)
        credentials = stage_home / "credentials.json"
        credentials.write_text("{}", encoding="utf-8")
        credentials.chmod(0o600)

    CredentialRefreshTransactions(store, root).recover()

    assert not any(path.is_dir() for path in root.iterdir())


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACL boundary")
def test_macos_extended_acl_provider_stage_remains_blocked(
    tmp_path: Path,
) -> None:
    """A private mode cannot hide an extended ACL on provider output."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    root = tmp_path / "credential-refresh"
    interrupted = CredentialRefreshTransactions(store, root)
    with interrupted.hold_stable(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.CREDENTIAL_REQUIRED.value,
        started_at=REFERENCE_TIME,
    ) as lease:
        stage_home = interrupted.prepare_provider_stage(lease)
        credentials = stage_home / "credentials.json"
        credentials.write_text("{}", encoding="utf-8")
        credentials.chmod(0o600)
        subprocess.run(
            (
                "/bin/chmod",
                "+a",
                "everyone allow read",
                str(credentials),
            ),
            check=True,
        )

    with pytest.raises(CredentialRefreshRecoveryBlockedError):
        CredentialRefreshTransactions(store, root).recover()

    assert credentials.is_file()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="Linux provider-stage mode normalization",
)
def test_recovery_skips_one_active_provider_stage_and_resolves_another(
    tmp_path: Path,
) -> None:
    """Recovery never scans or mutates another account's locked child home."""
    active_label = AccountLabel("claude-active")
    stale_label = AccountLabel("claude-stale")
    store = make_account_store(
        tmp_path,
        (
            login_account(str(active_label), generation="active"),
            login_account(str(stale_label), generation="stale"),
        ),
    )
    root = tmp_path / "credential-refresh"
    active = CredentialRefreshTransactions(store, root)
    entered = Event()
    release = Event()
    active_directories: list[Path] = []
    active_modes: list[int] = []

    def hold_active_stage() -> None:
        with active.hold_stable(
            provider_id=ProviderId.CLAUDE,
            label=active_label,
            reason=CredentialRefreshReason.CREDENTIAL_REQUIRED.value,
            started_at=REFERENCE_TIME,
        ) as lease:
            stage_home = active.prepare_provider_stage(lease)
            backups = stage_home / ".claude" / "backups"
            backups.mkdir(mode=0o755)
            backups.chmod(0o755)
            active_directories.append(lease._directory)
            active_modes.append(backups.stat().st_mode)
            entered.set()
            release.wait()

    thread = Thread(target=hold_active_stage)
    thread.start()
    assert entered.wait(timeout=5)
    stale = CredentialRefreshTransactions(store, root)
    with stale.hold_stable(
        provider_id=ProviderId.CLAUDE,
        label=stale_label,
        reason=CredentialRefreshReason.CREDENTIAL_REQUIRED.value,
        started_at=REFERENCE_TIME,
    ) as stale_lease:
        stale.prepare_provider_stage(stale_lease)
        stale_directory = stale_lease._directory

    try:
        CredentialRefreshTransactions(store, root).recover()
        assert active_directories[0].is_dir()
        assert not stale_directory.exists()
        backups = active_directories[0] / "provider-home/.claude/backups"
        assert backups.stat().st_mode == active_modes[0]
    finally:
        release.set()
        thread.join(timeout=5)
    assert not thread.is_alive()

    CredentialRefreshTransactions(store, root).recover()
    assert not any(path.is_dir() for path in root.iterdir())


@pytest.mark.parametrize("remove_target", [False, True])
def test_complete_stage_recovery_does_not_resurrect_changed_target(
    tmp_path: Path,
    remove_target: bool,
) -> None:
    """Recovery discards a stale stage after explicit target mutation."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    root = tmp_path / "credential-refresh"
    coordinator = CredentialRefreshCoordinator(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: RefreshProvider()},
        CredentialRefreshTransactions(
            store,
            root,
            faults=CrashAt(CredentialRefreshCrashPoint.STAGE_COMPLETE),
        ),
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(store),
    )
    with pytest.raises(SimulatedCrashError):
        coordinator.refresh(
            provider_id=ProviderId.CLAUDE,
            label=label,
            reason=CredentialRefreshReason.OPERATOR_FORCED,
        )
    external = make_account_store(tmp_path)
    if remove_target:
        remove_saved_account(external, label)
    else:
        external.persist(login_account(generation="manual"))

    CredentialRefreshTransactions(store, root).recover()

    saved = store.get(str(label))
    if remove_target:
        assert saved is None
    else:
        assert saved is not None
        assert saved.access_token == "sk-ant-oat01-manual"
    assert not any(path.is_dir() for path in root.iterdir())


@pytest.mark.parametrize("unsafe_kind", ["malformed", "oversized", "mode"])
def test_malformed_unsafe_or_oversized_recovery_fails_closed(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    """Untrusted private evidence never falls through to a refresh."""
    store = make_account_store(tmp_path, (login_account(),))
    root = tmp_path / "credential-refresh"
    root.mkdir(mode=0o700)
    directory = root / ("a" * 64)
    directory.mkdir(mode=0o700)
    journal = directory / "intent.json"
    payload = (
        b"x" * (MAX_PRIVATE_FILE_BYTES + 1)
        if unsafe_kind == "oversized"
        else b"{"
    )
    journal.write_bytes(payload)
    journal.chmod(0o644 if unsafe_kind == "mode" else 0o600)

    with pytest.raises(CredentialRefreshRecoveryBlockedError) as raised:
        CredentialRefreshTransactions(store, root).recover()

    assert raised.value.next_command == ("sidekick-usages", "doctor")
    assert journal.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link fixture")
def test_linked_recovery_evidence_fails_closed(tmp_path: Path) -> None:
    """A second name for journal bytes blocks recovery without deletion."""
    store = make_account_store(tmp_path, (login_account(),))
    root = tmp_path / "credential-refresh"
    root.mkdir(mode=0o700)
    directory = root / ("b" * 64)
    directory.mkdir(mode=0o700)
    journal = directory / "intent.json"
    journal.write_bytes(b"{")
    journal.chmod(0o600)
    partner = tmp_path / "journal-partner"
    partner.hardlink_to(journal)

    with pytest.raises(CredentialRefreshRecoveryBlockedError):
        CredentialRefreshTransactions(store, root).recover()

    assert journal.exists()
    assert partner.exists()


def test_durability_uncertainty_retains_evidence_and_blocks_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uncertain account commit cannot be retried or discarded."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    root = tmp_path / "credential-refresh"

    def uncertain(*_arguments: object, **_keywords: object) -> Never:
        raise DurabilityUncertainError("accounts.json")

    monkeypatch.setattr(store, "merge_credential_refresh", uncertain)
    coordinator = CredentialRefreshCoordinator(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: RefreshProvider()},
        CredentialRefreshTransactions(store, root),
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(store),
    )

    with pytest.raises(CredentialRefreshRecoveryBlockedError):
        coordinator.refresh(
            provider_id=ProviderId.CLAUDE,
            label=label,
            reason=CredentialRefreshReason.OPERATOR_FORCED,
        )

    with pytest.raises(CredentialRefreshRecoveryBlockedError):
        CredentialRefreshTransactions(store, root).recover()
    assert next(root.glob("*/intent.json")).exists()
    assert next(root.glob("*/replacement.json")).exists()

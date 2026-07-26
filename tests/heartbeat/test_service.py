"""Heartbeat service behavior tests."""

from datetime import timedelta, timezone
from pathlib import Path

import pytest

from sidekick_usages.core.types import HeartbeatStatus, ProviderId
from sidekick_usages.heartbeat.models import (
    HeartbeatProbeResult,
    UsageWindowState,
)
from sidekick_usages.heartbeat.ports import HeartbeatProvider
from sidekick_usages.heartbeat.service import HeartbeatService
from sidekick_usages.http.client import HttpClient
from tests.fakes.heartbeat import (
    ROUNDTRIP_AUDIT_TIME,
    SPARK_RESET,
    STANDARD_RESET,
    FakeHeartbeatProvider,
    heartbeat_account,
)
from tests.support.accounts import RuntimeCredentialResolver
from tests.support.persistence import make_account_store
from tests.support.time import REFERENCE_TIME, FixedClock


def test_heartbeat_reset_models_require_aware_utc_datetimes() -> None:
    """Heartbeat boundary results normalize aware time and reject naive."""
    offset = REFERENCE_TIME.astimezone(timezone(timedelta(hours=-4)))
    results = (
        UsageWindowState(active=True, reset_at=offset),
        HeartbeatProbeResult(
            status=HeartbeatStatus.ACTIVE,
            message="active",
            warmed=False,
            reset_at=offset,
        ),
    )

    assert tuple(result.reset_at for result in results) == (
        REFERENCE_TIME,
        REFERENCE_TIME,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        UsageWindowState(
            active=True,
            reset_at=REFERENCE_TIME.replace(tzinfo=None),
        )


def test_account_roundtrips_heartbeat_metadata(tmp_path: Path) -> None:
    """Heartbeat settings and diagnostics persist in the account store."""
    store = make_account_store(
        tmp_path,
        [
            heartbeat_account(
                heartbeat_enabled=True,
                heartbeat_window_resets={
                    "standard": STANDARD_RESET,
                    "spark": SPARK_RESET,
                },
                heartbeat_targets=("standard", "spark"),
            )
        ],
    )
    account = store.get("team")
    assert account is not None
    account.last_heartbeat_at = ROUNDTRIP_AUDIT_TIME
    account.last_heartbeat_status = HeartbeatStatus.WARMED
    account.last_heartbeat_error = None
    store.persist(account)

    restored = make_account_store(tmp_path).get("team")

    assert restored is not None
    assert restored.heartbeat_enabled is True
    assert restored.heartbeat_window_resets == {
        "standard": STANDARD_RESET,
        "spark": SPARK_RESET,
    }
    assert restored.heartbeat_targets == ("standard", "spark")
    assert restored.last_heartbeat_at == ROUNDTRIP_AUDIT_TIME
    assert restored.last_heartbeat_status is HeartbeatStatus.WARMED
    assert restored.last_heartbeat_error is None


def test_heartbeat_all_skips_disabled_accounts(tmp_path: Path) -> None:
    """Scheduler mode only probes accounts explicitly enabled."""
    provider = FakeHeartbeatProvider()
    store = make_account_store(
        tmp_path,
        [heartbeat_account(heartbeat_enabled=False)],
    )
    service = HeartbeatService(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(store),
    )

    outcomes = service.heartbeat_all()

    assert provider.heartbeat_calls == []
    assert outcomes[0].status is HeartbeatStatus.DISABLED


def test_heartbeat_service_owns_support_display_and_explicit_empty_mapping(
    tmp_path: Path,
) -> None:
    account = heartbeat_account()
    store = make_account_store(tmp_path, [account])
    injected: dict[ProviderId, HeartbeatProvider] = {}
    empty = HeartbeatService(
        store,
        HttpClient(),
        injected,
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(store),
    )
    injected[ProviderId.CLAUDE] = FakeHeartbeatProvider()

    assert empty.support_label(account) == "unsupported"
    assert empty.support_labels((account,)) == {"team": "unsupported"}

    configured = HeartbeatService(
        store,
        HttpClient(),
        injected,
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(store),
    )
    assert configured.support_label(account) == "off"
    account.last_heartbeat_status = HeartbeatStatus.FAILED
    assert configured.support_labels((account,)) == {"team": "needs-login"}


def test_heartbeat_label_runs_even_when_disabled(tmp_path: Path) -> None:
    """Explicit label mode is a one-shot warm request."""
    provider = FakeHeartbeatProvider()
    store = make_account_store(
        tmp_path,
        [heartbeat_account(heartbeat_enabled=False)],
    )
    service = HeartbeatService(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(store),
    )

    outcome = service.heartbeat_account(
        store.get("team"), require_enabled=False
    )

    assert outcome.status is HeartbeatStatus.WARMED
    assert provider.heartbeat_calls == [("team", "old-token")]
    saved = make_account_store(tmp_path).get("team")
    assert saved is not None
    assert saved.last_heartbeat_status is HeartbeatStatus.WARMED
    assert saved.heartbeat_window_resets == {"standard": STANDARD_RESET}


def test_heartbeat_decision_samples_clock_once(tmp_path: Path) -> None:
    """Auth and cached-reset checks share one heartbeat reference time."""
    provider = FakeHeartbeatProvider()
    clock = FixedClock()
    store = make_account_store(
        tmp_path,
        [
            heartbeat_account(
                heartbeat_enabled=True,
                access_expiry_at=REFERENCE_TIME + timedelta(hours=1),
                heartbeat_window_resets={"standard": STANDARD_RESET},
            )
        ],
    )
    service = HeartbeatService(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        clock=clock,
        resolver=RuntimeCredentialResolver(store),
    )

    outcome = service.heartbeat_account(store.get("team"))

    assert outcome.status is HeartbeatStatus.ACTIVE
    assert provider.heartbeat_calls == []
    assert clock.calls == 1


def test_heartbeat_cache_is_target_specific(tmp_path: Path) -> None:
    """A cached Spark reset must not suppress a standard Codex warm."""
    provider = FakeHeartbeatProvider(provider_id=ProviderId.CODEX)
    clock = FixedClock()
    store = make_account_store(
        tmp_path,
        [
            heartbeat_account(
                provider_id=ProviderId.CODEX,
                heartbeat_enabled=True,
                heartbeat_window_resets={
                    "spark": STANDARD_RESET,
                },
            )
        ],
    )
    service = HeartbeatService(
        store,
        HttpClient(),
        {ProviderId.CODEX: provider},
        clock=clock,
        resolver=RuntimeCredentialResolver(store),
    )

    outcome = service.heartbeat_account(
        store.get("team"), target_id="standard"
    )

    assert outcome.status is HeartbeatStatus.WARMED
    assert provider.heartbeat_calls == [("team", "old-token")]


def test_heartbeat_persists_failure_per_account(tmp_path: Path) -> None:
    """One provider failure is recorded instead of escaping."""
    provider = FakeHeartbeatProvider(
        heartbeat_results=[
            HeartbeatProbeResult(
                status=HeartbeatStatus.FAILED,
                message="rate limited",
                action_required=True,
                warmed=False,
            )
        ]
    )
    store = make_account_store(
        tmp_path,
        [heartbeat_account(heartbeat_enabled=True)],
    )
    clock = FixedClock()
    service = HeartbeatService(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        clock=clock,
        resolver=RuntimeCredentialResolver(store),
    )

    outcome = service.heartbeat_account(store.get("team"))

    assert outcome.status is HeartbeatStatus.FAILED
    assert outcome.action_required is True
    saved = make_account_store(tmp_path).get("team")
    assert saved is not None
    assert saved.last_heartbeat_at == REFERENCE_TIME
    assert saved.last_heartbeat_status is HeartbeatStatus.FAILED
    assert saved.last_heartbeat_error == "provider_failure"

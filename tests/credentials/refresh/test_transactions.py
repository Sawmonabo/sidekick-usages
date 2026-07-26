"""Serialized saved-credential refresh transaction tests."""

from datetime import timedelta
from pathlib import Path
from threading import Barrier, Event, Thread

import pytest

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId, RefreshStatus
from sidekick_usages.credentials.models import (
    CredentialRefreshResult,
    CredentialRefreshSuccess,
)
from sidekick_usages.credentials.refresh import (
    CredentialRefreshCoordinator,
    CredentialRefreshReason,
)
from sidekick_usages.http.client import HttpClient
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
    RefreshSuccess,
)
from tests.fakes.credentials.refresh import (
    BlockingRefreshProvider,
    BoundaryRecordingRefreshTransactions,
    CallbackRefreshProvider,
    ParallelRefreshProvider,
    RefreshProvider,
    login_account,
    refresh_coordinator,
)
from tests.support.accounts import RuntimeCredentialResolver
from tests.support.persistence import make_account_store, remove_saved_account
from tests.support.time import REFERENCE_TIME, FixedClock

_TWO_CALLERS = 2


def test_setup_token_returns_manual_action_without_lock_or_provider(
    tmp_path: Path,
) -> None:
    """A setup token is explicit manual work, never a rotating exchange."""
    label = AccountLabel("claude-setup")
    store = make_account_store(
        tmp_path,
        (
            Account(
                label=label,
                credentials=ClaudeSetupTokenCredentials(
                    access_token="sk-ant-oat01-setup"
                ),
            ),
        ),
    )
    refresh_root = tmp_path / "credential-refresh"
    persistence = BoundaryRecordingRefreshTransactions(store, refresh_root)
    provider = RefreshProvider()
    coordinator = CredentialRefreshCoordinator(
        store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        persistence,
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(store),
    )

    result = coordinator.refresh(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    assert isinstance(result, ProviderFailure)
    assert result.provider_id is ProviderId.CLAUDE
    assert result.kind is ProviderFailureKind.MISSING
    assert result.action_required is True
    assert persistence.crossings == []
    assert provider.calls == []
    assert not refresh_root.exists()


def test_operator_forced_refresh_commits_one_targeted_replacement(
    tmp_path: Path,
) -> None:
    """The concrete transaction owns the durable rotating refresh."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    provider = RefreshProvider()
    coordinator = refresh_coordinator(
        store,
        provider,
        tmp_path / "credential-refresh",
    )

    result = coordinator.refresh(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    assert not isinstance(result, ProviderFailure)
    assert result.label == label
    assert len(provider.calls) == 1
    saved = store.get(str(label))
    assert saved is not None
    assert saved.access_token == "sk-ant-oat01-new"
    assert saved.refresh_token == "refresh-new"
    assert saved.plan == "max"
    assert saved.last_refresh_status is not None
    assert saved.last_refresh_error is None


def test_same_credential_has_one_exchange_and_waiter_reacquires_new_lock(
    tmp_path: Path,
) -> None:
    """A stale waiter never calls the provider under the old-token lock."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    entered = Event()
    release = Event()
    provider = BlockingRefreshProvider(entered, release)
    root = tmp_path / "credential-refresh"
    coordinator = refresh_coordinator(store, provider, root)
    results: list[CredentialRefreshResult] = []
    failures: list[BaseException] = []

    def refresh() -> None:
        try:
            results.append(
                coordinator.refresh(
                    provider_id=ProviderId.CLAUDE,
                    label=label,
                    reason=CredentialRefreshReason.SCHEDULED_DUE,
                )
            )
        except BaseException as error:
            failures.append(error)

    first = Thread(target=refresh)
    second = Thread(target=refresh)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert failures == []
    assert len(results) == _TWO_CALLERS
    assert len(provider.calls) == 1
    operation_locks = tuple(root.glob("*.refresh.lock"))
    assert len(operation_locks) == _TWO_CALLERS


def test_stabilization_uses_authority_written_by_independent_store(
    tmp_path: Path,
) -> None:
    """Fresh preflight avoids ever acquiring the stale operation lock."""
    label = AccountLabel("claude-team")
    stale_store = make_account_store(tmp_path, (login_account(),))
    current_store = make_account_store(tmp_path)
    current_store.persist(login_account(generation="rotated"))
    provider = RefreshProvider()
    root = tmp_path / "credential-refresh"
    coordinator = refresh_coordinator(stale_store, provider, root)

    result = coordinator.refresh(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    assert not isinstance(result, ProviderFailure)
    assert len(provider.calls) == 1
    assert provider.calls[0].access_token == "sk-ant-oat01-rotated"
    assert provider.calls[0].refresh_token == "refresh-rotated"
    assert len(tuple(root.glob("*.refresh.lock"))) == 1


def test_different_refresh_credentials_exchange_concurrently(
    tmp_path: Path,
) -> None:
    """Independent operation identities do not serialize provider I/O."""
    first_label = AccountLabel("claude-first")
    second_label = AccountLabel("claude-second")
    store = make_account_store(
        tmp_path,
        (
            login_account(str(first_label), generation="first"),
            login_account(str(second_label), generation="second"),
        ),
    )
    provider = ParallelRefreshProvider(Barrier(2))
    coordinator = refresh_coordinator(
        store,
        provider,
        tmp_path / "credential-refresh",
    )
    results: list[CredentialRefreshResult] = []
    failures: list[BaseException] = []

    def refresh(label: AccountLabel) -> None:
        try:
            results.append(
                coordinator.refresh(
                    provider_id=ProviderId.CLAUDE,
                    label=label,
                    reason=CredentialRefreshReason.OPERATOR_FORCED,
                )
            )
        except BaseException as error:
            failures.append(error)

    threads = (
        Thread(target=refresh, args=(first_label,)),
        Thread(target=refresh, args=(second_label,)),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert len(results) == _TWO_CALLERS
    assert {str(call.label) for call in provider.calls} == {
        str(first_label),
        str(second_label),
    }


def test_targeted_merge_rebases_unrelated_write_and_target_heartbeat(
    tmp_path: Path,
) -> None:
    """A refresh preserves fresh unrelated and target-owned metadata."""
    target_label = AccountLabel("claude-team")
    other_label = AccountLabel("claude-other")
    store = make_account_store(
        tmp_path,
        (
            login_account(str(target_label)),
            login_account(str(other_label), generation="other"),
        ),
    )

    def mutate_concurrently(account: Account) -> RefreshResult:
        external = make_account_store(tmp_path)
        other = external.get(str(other_label))
        assert other is not None
        other.plan = "enterprise"
        external.persist(other)
        target = external.get(str(target_label))
        assert target is not None
        target.heartbeat_enabled = True
        external.persist(target)
        previous = account.credentials
        assert isinstance(previous, ClaudeLoginCredentials)
        return RefreshSuccess(
            credentials=ClaudeLoginCredentials(
                access_token="sk-ant-oat01-new",
                refresh_token="refresh-new",
                access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=2)),
                refresh_expiry=UnknownExpiry(),
                scopes=previous.scopes,
            ),
            plan="max",
        )

    coordinator = refresh_coordinator(
        store,
        CallbackRefreshProvider(mutate_concurrently),
        tmp_path / "credential-refresh",
    )

    result = coordinator.refresh(
        provider_id=ProviderId.CLAUDE,
        label=target_label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    assert not isinstance(result, ProviderFailure)
    target = store.get(str(target_label))
    other = store.get(str(other_label))
    assert target is not None
    assert other is not None
    assert target.access_token == "sk-ant-oat01-new"
    assert target.heartbeat_enabled is True
    assert other.plan == "enterprise"


def test_provider_without_plan_preserves_concurrent_target_plan(
    tmp_path: Path,
) -> None:
    """Absent provider plan evidence never republishes a stale plan."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))

    def update_plan(account: Account) -> RefreshResult:
        external = make_account_store(tmp_path)
        target = external.get(str(label))
        assert target is not None
        target.plan = "enterprise"
        external.persist(target)
        previous = account.credentials
        assert isinstance(previous, ClaudeLoginCredentials)
        return RefreshSuccess(
            credentials=ClaudeLoginCredentials(
                access_token="sk-ant-oat01-new",
                refresh_token="refresh-new",
                access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=2)),
                refresh_expiry=UnknownExpiry(),
                scopes=previous.scopes,
            ),
            plan=None,
        )

    coordinator = refresh_coordinator(
        store,
        CallbackRefreshProvider(update_plan),
        tmp_path / "credential-refresh",
    )

    result = coordinator.refresh(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    assert isinstance(result, CredentialRefreshSuccess)
    saved = store.get(str(label))
    assert saved is not None
    assert saved.plan == "enterprise"


@pytest.mark.parametrize("remove_target", [False, True])
def test_changed_or_removed_target_is_never_resurrected(
    tmp_path: Path,
    remove_target: bool,
) -> None:
    """Provider output cannot recreate or overwrite a changed target."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))

    def replace_or_remove(account: Account) -> RefreshResult:
        external = make_account_store(tmp_path)
        if remove_target:
            remove_saved_account(external, label)
        else:
            replacement = login_account(generation="manual")
            external.persist(replacement)
        previous = account.credentials
        assert isinstance(previous, ClaudeLoginCredentials)
        return RefreshSuccess(
            credentials=ClaudeLoginCredentials(
                access_token="sk-ant-oat01-provider",
                refresh_token="refresh-provider",
                access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=2)),
                refresh_expiry=UnknownExpiry(),
                scopes=previous.scopes,
            )
        )

    coordinator = refresh_coordinator(
        store,
        CallbackRefreshProvider(replace_or_remove),
        tmp_path / "credential-refresh",
    )

    coordinator.refresh(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    saved = store.get(str(label))
    if remove_target:
        assert saved is None
    else:
        assert saved is not None
        assert saved.access_token == "sk-ant-oat01-manual"
        assert saved.refresh_token == "refresh-manual"


def test_late_failure_cannot_overwrite_a_newer_success(
    tmp_path: Path,
) -> None:
    """Failure diagnostics use the same exact credential guard."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))

    def succeed_elsewhere(account: Account) -> RefreshResult:
        external = make_account_store(tmp_path)
        replacement = login_account(generation="newer")
        replacement.last_refresh_at = REFERENCE_TIME
        replacement.last_refresh_status = RefreshStatus.OK
        replacement.last_refresh_error = None
        external.persist(replacement)
        return ProviderFailure(
            provider_id=account.provider_id,
            kind=ProviderFailureKind.REJECTED,
            message="Synthetic stale failure.",
        )

    coordinator = refresh_coordinator(
        store,
        CallbackRefreshProvider(succeed_elsewhere),
        tmp_path / "credential-refresh",
    )

    result = coordinator.refresh(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    assert isinstance(result, CredentialRefreshSuccess)
    saved = store.get(str(label))
    assert saved is not None
    assert saved.access_token == "sk-ant-oat01-newer"
    assert saved.last_refresh_status is RefreshStatus.OK
    assert saved.last_refresh_error is None


def test_target_disappearance_during_stabilization_is_typed(
    tmp_path: Path,
) -> None:
    """A post-lock disappearance returns the closed refresh result type."""
    label = AccountLabel("claude-team")
    stale_store = make_account_store(tmp_path, (login_account(),))
    external = make_account_store(tmp_path)
    remove_saved_account(external, label)
    provider = RefreshProvider()
    coordinator = refresh_coordinator(
        stale_store,
        provider,
        tmp_path / "credential-refresh",
    )

    result = coordinator.refresh(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.MISSING
    assert provider.calls == []


def test_bounded_stabilization_exhaustion_is_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated durable changes return a retryable closed outcome."""
    label = AccountLabel("claude-team")
    store = make_account_store(tmp_path, (login_account(),))
    external = make_account_store(tmp_path)
    provider = RefreshProvider()
    original_read = store.read_fresh
    generation = 0

    def keep_changing(
        requested: AccountLabel,
        *,
        provider_id: ProviderId | None = None,
    ) -> Account | None:
        nonlocal generation
        generation += 1
        external.persist(login_account(generation=f"race-{generation}"))
        return original_read(requested, provider_id=provider_id)

    monkeypatch.setattr(store, "read_fresh", keep_changing)
    coordinator = refresh_coordinator(
        store,
        provider,
        tmp_path / "credential-refresh",
    )

    result = coordinator.refresh(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.UNREADABLE
    assert result.action_required is False
    assert provider.calls == []

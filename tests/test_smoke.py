"""Package import and composition-root smoke tests."""

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import click
import pytest

import sidekick_usages
from sidekick_usages.cli import context
from sidekick_usages.cli.context import (
    Composed,
    DoctorFailed,
    DoctorReady,
    InvocationContext,
    UpdateContext,
    compose_app_context,
    compose_doctor_context,
    default_invocation_composers,
)
from sidekick_usages.core.models import Account, ClaudeSetupTokenCredentials
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.doctor.accounts.models import HeartbeatSupport
from sidekick_usages.http.client import HttpClient
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import ManagedFileReadError
from sidekick_usages.update import UpdateService
from tests.test_support import make_account_store, make_application_paths


class _RecordingHttpClient(HttpClient):
    """Record close calls and optionally fail cleanup."""

    def __init__(self, *, cleanup_error: RuntimeError | None = None) -> None:
        super().__init__()
        self.close_calls = 0
        self.cleanup_error = cleanup_error

    def close(self) -> None:
        self.close_calls += 1
        super().close()
        if self.cleanup_error is not None:
            raise self.cleanup_error


def test_package_version_is_set() -> None:
    assert sidekick_usages.__version__


def test_failed_composition_closes_resources_without_masking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _RecordingHttpClient()
    failure = RuntimeError("composition sentinel")
    monkeypatch.setattr(
        context,
        "HttpClient",
        lambda *, clock: client,
    )

    def fail_load(_store: AccountStore) -> None:
        raise failure

    monkeypatch.setattr(AccountStore, "load", fail_load)
    with pytest.raises(RuntimeError) as raised:
        compose_app_context(
            paths=make_application_paths(tmp_path),
            providers={},
            heartbeat_providers={},
        )

    assert raised.value is failure
    assert client.close_calls == 1


def test_lazy_composition_caches_and_closes_once() -> None:
    close_events: list[str] = []
    resources = ExitStack()
    http = resources.enter_context(HttpClient())
    resources.callback(close_events.append, "closed")
    owner = Composed(UpdateContext(UpdateService(http)), resources)
    compose_calls = 0

    def compose() -> Composed[UpdateContext]:
        nonlocal compose_calls
        compose_calls += 1
        return owner

    invocation = InvocationContext(
        composers=replace(
            default_invocation_composers(),
            update=compose,
        )
    )
    click_context = click.Context(click.Command("lifecycle"))

    assert invocation.require_update(click_context) is owner.value
    assert invocation.require_update(click_context) is owner.value
    assert compose_calls == 1
    click_context.close()
    click_context.close()
    owner.close()
    assert close_events == ["closed"]


def test_composition_honors_empty_provider_maps_and_current_store(
    tmp_path: Path,
) -> None:
    account = Account(
        label=AccountLabel("explicit-empty"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="test-only-access"
        ),
        plan="max",
    )
    make_account_store(tmp_path, (account,))
    paths = make_application_paths(tmp_path)
    application = compose_app_context(
        paths=paths,
        providers={},
        heartbeat_providers={},
    )
    doctor = compose_doctor_context(
        paths=paths,
        providers={},
        heartbeat_providers={},
    )
    try:
        prompt = application.value.credentials.prompt_spec(ProviderId.CLAUDE)
        saved = application.value.accounts.get("explicit-empty")
        state = doctor.value.state
        assert getattr(prompt, "kind", None) is not None
        assert saved is not None
        assert application.value.heartbeat.support_label(saved) == (
            "unsupported"
        )
        assert isinstance(state, DoctorReady)
        assert (
            state.service.diagnostics()[0].heartbeat_support
            is HeartbeatSupport.UNSUPPORTED
        )
    finally:
        doctor.close()
        application.close()


def test_doctor_translates_current_store_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = make_application_paths(tmp_path)
    failure = ManagedFileReadError(paths.accounts.name)

    def fail_load(_store: AccountStore) -> None:
        raise failure

    monkeypatch.setattr(AccountStore, "load", fail_load)
    owner = compose_doctor_context(
        paths=paths,
        providers={},
        heartbeat_providers={},
    )
    try:
        state = owner.value.state
        assert isinstance(state, DoctorFailed)
        assert state.failure.code is failure.code
        assert state.failure.path == paths.accounts
    finally:
        owner.close()

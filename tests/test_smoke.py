"""Package import and composition-root smoke tests."""

import io
import json
from contextlib import ExitStack
from pathlib import Path

import click
import pytest
from rich.console import Console

import sidekick_usages
from sidekick_usages.cli import context as cli_context_module
from sidekick_usages.cli.context import (
    Composed,
    DoctorFailed,
    DoctorReady,
    InvocationContext,
    UpdateContext,
    compose_app_context,
    compose_doctor_context,
)
from sidekick_usages.core.models import Account, ClaudeCredentials
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.errors import ManagedFileReadError
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.migrations import PersistenceMigrationService
from sidekick_usages.providers.base import ProviderFailure, ProviderFailureKind
from sidekick_usages.providers.registry import (
    build_heartbeat_registry,
    build_provider_registry,
)
from sidekick_usages.update import UpdateService
from tests.test_support import (
    CliHarness,
    FixedClock,
    make_account_store,
    make_application_paths,
)


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
    """The installed-version facade exposes one non-empty version."""
    assert isinstance(sidekick_usages.__version__, str)
    assert sidekick_usages.__version__


def _failing_composition(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: _RecordingHttpClient,
    failure: RuntimeError,
) -> None:
    paths = make_application_paths(tmp_path)
    PersistenceFilesystem(paths.accounts.canonical).repair_parent_permissions()
    monkeypatch.setattr(
        cli_context_module,
        "HttpClient",
        lambda *, clock: client,
    )

    def fail_load(_store: AccountStore) -> None:
        raise failure

    monkeypatch.setattr(cli_context_module.AccountStore, "load", fail_load)
    compose_app_context(
        paths=paths,
        providers={},
        heartbeat_providers={},
    )


def test_failed_composition_closes_initialized_http_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Partial construction closes acquired resources without masking."""
    client = _RecordingHttpClient()
    failure = RuntimeError("composition sentinel")

    with pytest.raises(RuntimeError) as raised:
        _failing_composition(monkeypatch, tmp_path, client, failure)

    assert raised.value is failure
    assert client.close_calls == 1


def test_failed_cleanup_preserves_construction_error_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dual failure exposes construction before LIFO cleanup failure."""
    cleanup = RuntimeError("cleanup sentinel")
    client = _RecordingHttpClient(cleanup_error=cleanup)
    construction = RuntimeError("composition sentinel")

    with pytest.raises(BaseExceptionGroup) as raised:
        _failing_composition(monkeypatch, tmp_path, client, construction)

    assert raised.value.exceptions == (construction, cleanup)
    assert client.close_calls == 1


def test_lazy_composition_caches_and_closes_once() -> None:
    """Repeated access registers one root-owned close callback."""
    close_events: list[str] = []
    resources = ExitStack()
    http = resources.enter_context(HttpClient())
    resources.callback(close_events.append, "closed")
    owner = Composed(
        UpdateContext(UpdateService(http)),
        resources,
    )
    compose_calls = 0

    def compose() -> Composed[UpdateContext]:
        nonlocal compose_calls
        compose_calls += 1
        return owner

    invocation = InvocationContext(update_composer=compose)
    context = click.Context(click.Command("lifecycle"))

    assert invocation.require_update(context) is owner.value
    assert invocation.require_update(context) is owner.value
    assert compose_calls == 1
    context.close()
    context.close()
    owner.close()
    assert close_events == ["closed"]


def test_explicit_empty_provider_maps_remain_empty_in_composition(
    tmp_path: Path,
) -> None:
    """Empty injection never silently activates production adapters."""
    account = Account(
        label=AccountLabel("explicit-empty"),
        credentials=ClaudeCredentials(access_token="test-only-access"),
        plan="max",
    )
    make_account_store(tmp_path, (account,))
    paths = make_application_paths(tmp_path)
    providers = {}
    heartbeat_providers = {}
    application = compose_app_context(
        paths=paths,
        providers=providers,
        heartbeat_providers=heartbeat_providers,
    )
    doctor = compose_doctor_context(
        paths=paths,
        providers=providers,
        heartbeat_providers=heartbeat_providers,
    )
    try:
        prompt = application.value.credentials.prompt_spec(ProviderId.CLAUDE)
        assert isinstance(prompt, ProviderFailure)
        assert prompt.kind is ProviderFailureKind.UNSUPPORTED
        saved = application.value.accounts.get("explicit-empty")
        assert saved is not None
        assert application.value.heartbeat.support_label(saved) == (
            "unsupported"
        )
        state = doctor.value.state
        assert isinstance(state, DoctorReady)
        diagnostic = state.service.diagnostics()[0]
        assert diagnostic.manual_action_required is True
        assert diagnostic.heartbeat_supported is False
    finally:
        doctor.close()
        application.close()


def test_doctor_reads_snapshot_without_constructing_account_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Current-state doctor stays on its read-only composition path."""
    account = Account(
        label=AccountLabel("doctor-account"),
        credentials=ClaudeCredentials(access_token="test-only-access"),
        plan="max",
    )
    make_account_store(tmp_path, (account,))
    paths = make_application_paths(tmp_path)
    constructions = 0

    def reject_store(*_args: object, **_kwargs: object) -> None:
        nonlocal constructions
        constructions += 1
        raise AssertionError("doctor constructed AccountStore")

    monkeypatch.setattr(cli_context_module, "AccountStore", reject_store)
    clock = FixedClock()
    providers = build_provider_registry(clock)
    owner = compose_doctor_context(
        paths=paths,
        clock=clock,
        providers=providers,
        heartbeat_providers=build_heartbeat_registry(providers),
    )
    assert isinstance(owner.value.state, DoctorReady)
    output = io.StringIO()
    harness = CliHarness(
        console=Console(file=output, force_terminal=False, width=200),
        err_console=Console(file=io.StringIO(), force_terminal=False),
        doctor=owner.value,
    )
    try:
        result = harness.invoke(["doctor", "--json"])
    finally:
        owner.close()

    assert result.exit_code == 0
    assert constructions == 0
    assert json.loads(output.getvalue())["accounts"][0]["label"] == (
        "doctor-account"
    )


def test_doctor_translates_a_post_assessment_read_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A source change after assessment remains a closed doctor state."""
    paths = make_application_paths(tmp_path)
    PersistenceFilesystem(paths.accounts.canonical).repair_parent_permissions()
    failure = ManagedFileReadError(paths.accounts.canonical.name)

    def fail_read(
        _service: PersistenceMigrationService,
    ) -> tuple[Account, ...]:
        raise failure

    monkeypatch.setattr(
        PersistenceMigrationService,
        "read_accounts",
        fail_read,
    )

    owner = compose_doctor_context(
        paths=paths,
        providers={},
        heartbeat_providers={},
    )
    try:
        state = owner.value.state
        assert isinstance(state, DoctorFailed)
        assert state.failure.code is failure.code
        assert state.failure.safe_path == paths.accounts.canonical
    finally:
        owner.close()

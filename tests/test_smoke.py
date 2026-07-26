"""Package import and composition-root smoke tests."""

from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

import click
import pytest

import sidekick_usages
from sidekick_usages.cli.context import InvocationContext
from sidekick_usages.cli.contexts import composition
from sidekick_usages.cli.contexts.composition import (
    compose_app_context,
    compose_doctor_context,
    default_invocation_composers,
)
from sidekick_usages.cli.contexts.models import (
    Composed,
    DoctorFailed,
    DoctorReady,
    UpdateContext,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
)
from sidekick_usages.core.models import Account, ClaudeSetupTokenCredentials
from sidekick_usages.core.selection.models import SelectedAccountState
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.doctor.accounts.models import HeartbeatSupport
from sidekick_usages.http.client import HttpClient
from sidekick_usages.persistence.accounts.index import AccountIndexReader
from sidekick_usages.persistence.accounts.runtime_bridge import (
    active_stored_reference,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.credentials.repository import (
    CredentialAuthorityRepository,
)
from sidekick_usages.persistence.errors import ManagedFileReadError
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
)
from sidekick_usages.persistence.types.error import PersistenceCode
from sidekick_usages.update import UpdateService
from tests.fakes.daemon.capabilities import (
    StaticProviderCapabilityService,
    make_provider_capability_report,
)
from tests.support.persistence import (
    make_account_store,
    make_account_store_with_private,
    make_application_paths,
)
from tests.support.time import REFERENCE_TIME


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
        composition,
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
    monkeypatch: pytest.MonkeyPatch,
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
    capability_service = StaticProviderCapabilityService(
        make_provider_capability_report()
    )
    monkeypatch.setattr(
        composition,
        "build_provider_capability_service",
        lambda _paths: capability_service,
    )
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


def test_doctor_fails_closed_for_untrusted_persisted_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = make_application_paths(tmp_path)
    failure = ManagedFileReadError(paths.accounts.name)
    capability_service = StaticProviderCapabilityService(
        make_provider_capability_report()
    )
    monkeypatch.setattr(
        composition,
        "build_provider_capability_service",
        lambda _paths: capability_service,
    )
    observe_accounts = AccountIndexReader.observe

    def fail_observe(_reader: AccountIndexReader) -> None:
        raise failure

    monkeypatch.setattr(AccountIndexReader, "observe", fail_observe)
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
    monkeypatch.setattr(
        AccountIndexReader,
        "observe",
        observe_accounts,
    )
    account = Account(
        label=AccountLabel("identity-mismatch"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="test-only-mismatch"
        ),
    )
    store, private = make_account_store_with_private(tmp_path, (account,))
    saved = store.saved_accounts()[0]
    SelectedStateStore(paths.selected_state).save(
        SelectedAccountState(
            provider_id=ProviderId.CLAUDE,
            runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
            account_id=saved.account_id,
            provider_identity=ProviderIdentity("unrelated-identity"),
            runtime_generation=AuthorityGeneration(
                "unrelated-generation"
            ),
            verified_at=REFERENCE_TIME,
            outcome=ActivationOutcome.VERIFIED,
        )
    )

    mismatched = compose_doctor_context(
        paths=paths,
        providers={},
        heartbeat_providers={},
    )
    try:
        state = mismatched.value.state
        assert isinstance(state, DoctorFailed)
        assert state.failure.code is PersistenceCode.INVALID_SCHEMA
        assert state.failure.message == (
            "Supervisor state does not match the saved accounts."
        )
    finally:
        mismatched.close()
    repository = CredentialAuthorityRepository(private)
    private.destroy_owned_directory(
        repository.bundle_path(
            saved.account_id,
            active_stored_reference(saved),
        )
    )
    missing_private = compose_doctor_context(
        paths=paths,
        providers={},
        heartbeat_providers={},
    )
    try:
        state = missing_private.value.state
        assert isinstance(state, DoctorFailed)
        assert state.failure.code is PersistenceCode.INVALID_SCHEMA
        assert state.failure.path == paths.accounts
    finally:
        missing_private.close()

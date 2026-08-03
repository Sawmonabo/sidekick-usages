"""Load-bearing managed Codex activation tests."""

from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    FinalizedSelection,
    ProviderAuthObservation,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    ActivationPhase,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.models.protocol import FailedPayload
from sidekick_usages.daemon.types.protocol import (
    EventKind,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.providers.codex.auth.storage import observe_native_auth
from sidekick_usages.providers.codex.broker.authority import (
    CodexSavedAuthorityResolver,
)
from sidekick_usages.providers.codex.broker.daemon import CodexDaemonManager
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.models import CodexDaemonAuthority
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from tests.fakes.codex.app_server.daemon import FakeCodexDaemon
from tests.fakes.codex.app_server.executable import (
    configure_codex_daemon_lifecycle,
)
from tests.fakes.codex.broker.runtime import (
    ACCOUNT_A_ID,
    ACCOUNT_A_PROVIDER_IDENTITY,
    GENERATION,
    MANAGED_ACCOUNT_ID,
    NATIVE_AUTH_SENTINEL,
    NEXT_GENERATION,
    PROVIDER_IDENTITY,
    account_store,
    activation_source_fixture,
    interrupt_activation_at_install,
    real_worker_executable,
    selected_account,
    wait_for_projected_generation,
)
from tests.fakes.codex.broker.supervisor import FakeCodexSupervisor
from tests.support.platform import REQUIRES_MANAGED_RUNTIME
from tests.support.time import REFERENCE_TIME

pytestmark = REQUIRES_MANAGED_RUNTIME

_CLAUDE_ACCOUNT_ID = SidekickAccountId("55555555-5555-4555-8555-555555555555")
_FIRST_ACTIVATION_ID = OperationId("88888888-8888-4888-8888-888888888888")


def _require_selected(
    selected: SelectedStateStore,
    account_id: SidekickAccountId,
    provider_identity: str,
    generation: str,
) -> FinalizedSelection:
    state = selected.load(ProviderId.CODEX)
    assert state is not None
    assert state.account_id == account_id
    assert state.generation == AuthorityGeneration(generation)
    observation = RuntimeAuthObservationStore(
        selected.path.parent / "operations"
    ).observe_projection(ProviderId.CODEX)
    if observation is not None:
        assert observation.provider_identity == ProviderIdentity(
            provider_identity
        )
    return state


def _require_projection_without_finalization(
    selected: SelectedStateStore,
    baseline: FinalizedSelection,
    provider_identity: str,
) -> ProviderAuthObservation:
    """Require provider proof while the coordinator pointer stays stable."""
    assert selected.load(ProviderId.CODEX) == baseline
    observation = RuntimeAuthObservationStore(
        selected.path.parent / "operations"
    ).observe_projection(ProviderId.CODEX)
    assert observation is not None
    assert observation.provider_identity == ProviderIdentity(provider_identity)
    return observation


def _codex_recovery_state(
    paths: ApplicationPaths,
) -> tuple[SelectedStateStore, ActivationJournalStore]:
    """Seed the selected baseline and return both recovery stores."""
    selected = SelectedStateStore(paths.selected_state)
    current = selected.load(ProviderId.CODEX)
    assert current is not None
    selected.compare_and_swap(
        replace(
            selected_account(
                ACCOUNT_A_ID,
                ACCOUNT_A_PROVIDER_IDENTITY,
                str(current.generation),
            ),
            epoch=current.epoch.next(),
        ),
        expected=current,
    )
    return (
        selected,
        ActivationJournalStore(
            paths.activation_journals,
            paths.durable_operations,
        ),
    )


def test_codex_activation_commits_only_correlated_target(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = activation_source_fixture(
        tmp_path,
        short_socket_root,
        monkeypatch,
    )
    selected = SelectedStateStore(fixture.paths.selected_state)
    selected_source = selected.load(ProviderId.CODEX)
    native_source = observe_native_auth(
        credential_home=fixture.native_home,
        observed_at=REFERENCE_TIME,
    )
    assert selected_source is not None
    saved_authority = CodexSavedAuthorityResolver(
        account_store(fixture.paths),
    )
    assert saved_authority.matches(selected_source, native_source)
    assert saved_authority.matches(
        selected_source,
        replace(
            native_source,
            generation=AuthorityGeneration(
                "access-token-sha256:refreshed-runtime-fingerprint"
            ),
        ),
    )
    assert (
        saved_authority.expectation(
            replace(
                selected_source,
                generation=AuthorityGeneration(NEXT_GENERATION),
            )
        )
        is None
    )
    assert not saved_authority.matches(
        selected_source,
        replace(
            native_source,
            provider_identity=ProviderIdentity(PROVIDER_IDENTITY),
        ),
    )
    claude = FinalizedSelection(
        provider_id=ProviderId.CLAUDE,
        account_id=_CLAUDE_ACCOUNT_ID,
        epoch=SelectionEpoch(1),
        generation=AuthorityGeneration("claude-generation"),
        finalized_at=REFERENCE_TIME,
    )
    selected.compare_and_swap(claude, expected=None)
    reject_revalidation = False
    revalidate = CodexDaemonManager.revalidate

    def revalidate_current_socket(
        manager: CodexDaemonManager,
        authority: CodexDaemonAuthority,
    ) -> None:
        if reject_revalidation:
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_CHANGED)
        revalidate(manager, authority)

    monkeypatch.setattr(
        CodexDaemonManager,
        "revalidate",
        revalidate_current_socket,
    )

    with FakeCodexDaemon(
        fixture.session_home,
        app_server_version="0.146.0",
    ) as daemon:
        configure_codex_daemon_lifecycle(
            fixture.provider_root,
            fixture.session_home,
            daemon.socket_path,
            app_server_version="0.146.0",
        )
        with FakeCodexSupervisor(
            fixture.paths,
            fixture.executable,
            fixture.session_home,
            fixture.environment,
            real_worker_executable(),
        ) as supervisor:
            supervisor.wait_until_ready()
            selected_baseline = selected.load(ProviderId.CODEX)
            assert selected_baseline is not None
            install_count = len(daemon.installed_account_ids)
            reject_revalidation = True
            client = ControlClient.connect(fixture.paths.supervisor_socket)
            failed = tuple(
                client.activate(ProviderId.CODEX, MANAGED_ACCOUNT_ID)
            )
            client.close()

            assert [event.kind for event in failed] == [
                EventKind.ACCEPTED,
                EventKind.PROGRESS,
                EventKind.FAILED,
            ]
            assert isinstance(failed[-1].payload, FailedPayload)
            assert len(daemon.installed_account_ids) > install_count
            assert selected.load(ProviderId.CODEX) == selected_baseline
            assert selected.load(ProviderId.CLAUDE) == claude
            interrupted = ActivationJournalStore(
                fixture.paths.activation_journals,
                fixture.paths.durable_operations,
            ).load(ProviderId.CODEX)
            assert interrupted.active is not None
            assert (
                interrupted.active.phase
                is ActivationPhase.RECONCILIATION_REQUIRED
            )

        reject_revalidation = False
        daemon.pause_next_install()
        with FakeCodexSupervisor(
            fixture.paths,
            fixture.executable,
            fixture.session_home,
            fixture.environment,
            real_worker_executable(),
        ) as restarted:
            daemon.wait_for_paused_install()
            daemon.resume_install()
            wait_for_projected_generation(
                fixture.paths,
                MANAGED_ACCOUNT_ID,
                PROVIDER_IDENTITY,
                NEXT_GENERATION,
            )
            _require_projection_without_finalization(
                selected,
                selected_baseline,
                PROVIDER_IDENTITY,
            )
            journal = ActivationJournalStore(
                fixture.paths.activation_journals,
                fixture.paths.durable_operations,
            ).load(ProviderId.CODEX)
            terminal = journal.history[-1]
            assert (
                restarted.ready,
                daemon.installed_account_ids[-1],
                journal.active,
                terminal.phase,
                terminal.target_authority_generation,
                terminal.verified_runtime_generation,
                selected.load(ProviderId.CLAUDE),
            ) == (
                False,
                PROVIDER_IDENTITY,
                None,
                ActivationPhase.COMMITTED,
                AuthorityGeneration(NEXT_GENERATION),
                AuthorityGeneration(NEXT_GENERATION),
                claude,
            )

    assert fixture.native_auth.read_bytes() == NATIVE_AUTH_SENTINEL


def test_codex_activation_recovers_at_official_mutation_boundary(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = activation_source_fixture(
        tmp_path,
        short_socket_root,
        monkeypatch,
    )
    selected, journals = _codex_recovery_state(fixture.paths)

    with FakeCodexDaemon(
        fixture.session_home,
        app_server_version="0.146.0",
    ) as daemon:
        configure_codex_daemon_lifecycle(
            fixture.provider_root,
            fixture.session_home,
            daemon.socket_path,
            app_server_version="0.146.0",
        )
        supervisor = FakeCodexSupervisor(
            fixture.paths,
            fixture.executable,
            fixture.session_home,
            fixture.environment,
            real_worker_executable(),
        )
        supervisor.start()
        supervisor.wait_until_ready()
        interrupt_activation_at_install(
            supervisor,
            daemon,
            fixture.paths,
            _FIRST_ACTIVATION_ID,
            MANAGED_ACCOUNT_ID,
        )

        assert daemon.installed_account_ids[-1] == PROVIDER_IDENTITY
        _require_selected(
            selected,
            ACCOUNT_A_ID,
            ACCOUNT_A_PROVIDER_IDENTITY,
            GENERATION,
        )
        assert journals.load(ProviderId.CODEX).active is not None
        installed_before_recovery = len(daemon.installed_account_ids)

        daemon.pause_next_install()
        with FakeCodexSupervisor(
            fixture.paths,
            fixture.executable,
            fixture.session_home,
            fixture.environment,
            real_worker_executable(),
        ) as restarted:
            daemon.wait_for_paused_install()
            client = ControlClient.connect(fixture.paths.supervisor_socket)
            retry = client.reconcile(ProviderId.CODEX)
            accepted = next(retry)
            assert accepted.kind is EventKind.ACCEPTED
            daemon.resume_install()
            assert tuple(retry)[-1].kind is EventKind.COMPLETED
            client.close()
            wait_for_projected_generation(
                fixture.paths,
                MANAGED_ACCOUNT_ID,
                PROVIDER_IDENTITY,
                NEXT_GENERATION,
            )

            assert (
                len(daemon.installed_account_ids) > installed_before_recovery
            )
            selected_before_finalization = selected.load(ProviderId.CODEX)
            assert selected_before_finalization is not None
            _require_projection_without_finalization(
                selected,
                selected_before_finalization,
                PROVIDER_IDENTITY,
            )
            recovered = journals.load(ProviderId.CODEX)
            assert recovered.active is None
            assert len(recovered.history) == 1
            assert (
                selected.load(ProviderId.CODEX) == selected_before_finalization
            )
            assert not restarted.ready
            assert daemon.installed_account_ids[-1] == PROVIDER_IDENTITY

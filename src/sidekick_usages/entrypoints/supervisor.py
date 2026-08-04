"""Resident supervisor console entry point."""

import os
import signal
import sys
import time
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from threading import Event
from types import FrameType

from sidekick_usages.clock import SystemClock
from sidekick_usages.core.selection.policy import protected_selection_enabled
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.dispatch import (
    OperationEventHub,
    SupervisorDispatcher,
)
from sidekick_usages.daemon.control.protocol import FramedTransport
from sidekick_usages.daemon.control.server import LocalControlServer
from sidekick_usages.daemon.lifecycle.constants import (
    CLAUDE_LAUNCHER_OPTION,
    CODEX_LAUNCHER_OPTION,
)
from sidekick_usages.daemon.models.worker import ProviderLaunchers
from sidekick_usages.daemon.runtime.codex import (
    DurableCodexOperationDispatcher,
)
from sidekick_usages.daemon.runtime.diagnostics import (
    CompositeOperationSink,
    ControlFailureDiagnosticSink,
    DiagnosticOperationSink,
    SanitizedDiagnosticLog,
)
from sidekick_usages.daemon.runtime.recovery import ActivationRecoveryScheduler
from sidekick_usages.daemon.runtime.scheduler import DurableScheduler
from sidekick_usages.daemon.runtime.supervisor import (
    SupervisorRuntime,
    WakeupChannel,
)
from sidekick_usages.daemon.selection.coordinator import SelectionCoordinator
from sidekick_usages.daemon.selection.recovery import SelectionRecovery
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
from sidekick_usages.daemon.selection.worker import (
    SelectionSchedulerSink,
    SelectionWorkerGateway,
)
from sidekick_usages.daemon.worker.exchange import WorkerExchangeRegistry
from sidekick_usages.daemon.worker.pool import (
    SubprocessWorkerLauncher,
    WorkerLaunchPlanner,
    WorkerPool,
    resolve_worker_executable,
)
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.persistence.accounts.reader import AccountIndexReader
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.observation import (
    RuntimeAuthObservationStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.results import WorkerResultStore
from sidekick_usages.persistence.supervisor.runtime import (
    RuntimeStateReader,
)
from sidekick_usages.persistence.supervisor.selection import (
    SelectedStateStore,
    SelectionOperationStore,
)
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.providers.claude.auth.storage.service import (
    claude_credential_basename,
)
from sidekick_usages.providers.claude.structured.data_plane import (
    ClaudeParticipantChannelRegistry,
    ClaudeProtectedCommitRelay,
    claude_participant_ack_required,
)
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable_from_launcher,
)
from sidekick_usages.providers.codex.auth.home import default_codex_home
from sidekick_usages.providers.codex.auth.storage import codex_auth_basename
from sidekick_usages.providers.codex.broker.responder import CodexRuntimeBroker
from sidekick_usages.providers.codex.broker.service import (
    CodexSharedRuntime,
    prepare_codex_session_home,
)

_EXIT_OK = 0
_INVALID_INVOCATION_EXIT_CODE = 2
_SUPERVISOR_ARGUMENT_PAIR_SIZE = 2
_SUPPORTED_SUPERVISOR_ARGUMENT_COUNTS = frozenset({0, 2, 4})


def _request_stop(stop: Event, wakeup: WakeupChannel) -> None:
    stop.set()
    wakeup.notify()


def _signal_stop(
    request_stop: Callable[[], None],
    _signal_number: int,
    _frame: FrameType | None,
) -> None:
    request_stop()


def _create_codex_runtime(
    paths: ApplicationPaths,
    launcher: Path | None,
    cancelled: Callable[[], bool],
) -> CodexSharedRuntime:
    session_home = prepare_codex_session_home(
        paths,
        lambda root: PrivateCredentialTree(
            root,
            account_path=paths.accounts,
        ),
        AccountIndexReader(paths.accounts).load,
        native_home=default_codex_home(),
        forbidden_entries=(
            codex_auth_basename(),
            claude_credential_basename(),
        ),
    )
    executable = discover_codex_executable_from_launcher(
        launcher,
        os.environ,
        cancelled=cancelled,
    )
    return CodexSharedRuntime.create(
        executable,
        session_home,
        environment=os.environ,
        cancelled=cancelled,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Compose and run one lean per-user resident supervisor."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        provider_launchers = parse_provider_launchers(arguments)
    except ValueError:
        return _INVALID_INVOCATION_EXIT_CODE
    paths = discover_application_paths()
    clock = SystemClock()
    wakeup = WakeupChannel()
    stop_requested = Event()
    queue = OperationQueueStore(paths.durable_operations)
    results = WorkerResultStore(paths.durable_operations)
    service_state = ServiceStateStore(paths.service_state)
    journals = ActivationJournalStore(
        paths.activation_journals,
        paths.durable_operations,
    )
    selected = SelectedStateStore(paths.selected_state)
    observations = RuntimeAuthObservationStore(paths.durable_operations)
    accounts = AccountStore(
        paths.accounts,
        PrivateCredentialTree(
            paths.private_credentials,
            account_path=paths.accounts,
        ),
    ).load()
    selection_journals = SelectionOperationStore(paths.selection_journals)
    exchanges = WorkerExchangeRegistry(time.monotonic)
    protected_channels = (
        ClaudeParticipantChannelRegistry(
            partial(
                claude_participant_ack_required,
                AccountIndexReader(paths.accounts).load,
            )
        )
        if protected_selection_enabled(ProviderId.CLAUDE)
        else None
    )
    participants = ParticipantRegistry(
        selected,
        attachments=protected_channels,
    )
    protected_relay = (
        ClaudeProtectedCommitRelay(exchanges, protected_channels)
        if protected_channels is not None
        else None
    )
    selection_workers = SelectionWorkerGateway(
        queue,
        clock,
        wakeup.notify,
        exchange_owner=protected_relay,
    )
    selection_recovery = SelectionRecovery(
        selected,
        selection_journals,
        participants,
        selection_workers,
        clock,
    )
    selection = SelectionCoordinator(
        selected,
        selection_journals,
        participants,
        selection_workers,
        clock,
        resume_recovery=selection_recovery.resume,
    )
    recovery = ActivationRecoveryScheduler(
        journals,
        queue,
        selection_recovery=selection_recovery,
    )
    diagnostic_log = SanitizedDiagnosticLog(paths.service_logs)
    control_diagnostics = ControlFailureDiagnosticSink(diagnostic_log, clock)
    events = OperationEventHub(control_diagnostics.failed)
    workers = WorkerPool(
        SubprocessWorkerLauncher(),
        WorkerLaunchPlanner(
            resolve_worker_executable(),
            os.environ,
            provider_launchers,
        ),
        wakeup.notify,
        exchanges=exchanges,
    )

    broker = CodexRuntimeBroker(
        partial(
            _create_codex_runtime,
            paths,
            provider_launchers.codex,
        ),
        RuntimeStateReader(
            ProviderId.CODEX,
            selected,
            journals,
            queue,
            observations,
            clock,
        ),
        accounts,
        DurableCodexOperationDispatcher(
            queue,
            observations,
            exchanges,
            clock.now,
            time.monotonic,
            wakeup.notify,
        ),
        exchanges,
        proof_transport_factory=FramedTransport,
        wall_time=clock.now,
        status_changed=wakeup.notify,
    )
    participants.add_attachment_registry(broker.participant_proofs)
    scheduler = DurableScheduler(
        queue,
        results,
        workers,
        clock,
        events=SelectionSchedulerSink(
            CompositeOperationSink(
                events,
                DiagnosticOperationSink(
                    diagnostic_log,
                    clock,
                    time.monotonic,
                ),
            ),
            selection_workers,
            selection_recovery,
        ),
        exchange_preparer=broker,
    )
    request_stop = partial(_request_stop, stop_requested, wakeup)
    dispatcher = SupervisorDispatcher(
        queue,
        service_state,
        events,
        clock,
        wakeup.notify,
        request_stop,
        selection=selection,
    )
    server = LocalControlServer(
        paths.runtime_directory,
        paths.supervisor_socket,
        dispatcher,
        failure_reporter=control_diagnostics.failed,
    )
    runtime = SupervisorRuntime(
        server,
        scheduler,
        recovery,
        service_state,
        clock,
        wakeup,
        stop_requested,
        broker,
    )
    signal.signal(signal.SIGTERM, partial(_signal_stop, request_stop))
    signal.signal(signal.SIGINT, partial(_signal_stop, request_stop))
    try:
        runtime.run()
    finally:
        if protected_channels is not None:
            protected_channels.close()
    return _EXIT_OK


def parse_provider_launchers(
    arguments: Sequence[str],
) -> ProviderLaunchers:
    """Parse the exact optional provider launcher service arguments."""
    if len(arguments) not in _SUPPORTED_SUPERVISOR_ARGUMENT_COUNTS:
        raise ValueError("Invalid supervisor invocation.")
    launcher_paths: dict[str, Path] = {}
    for index in range(
        0,
        len(arguments),
        _SUPERVISOR_ARGUMENT_PAIR_SIZE,
    ):
        option, raw_path = arguments[
            index : index + _SUPERVISOR_ARGUMENT_PAIR_SIZE
        ]
        if (
            option not in {CLAUDE_LAUNCHER_OPTION, CODEX_LAUNCHER_OPTION}
            or option in launcher_paths
            or not raw_path
        ):
            raise ValueError("Invalid supervisor invocation.")
        launcher_paths[option] = Path(raw_path)
    return ProviderLaunchers(
        claude=launcher_paths.get(CLAUDE_LAUNCHER_OPTION),
        codex=launcher_paths.get(CODEX_LAUNCHER_OPTION),
    )

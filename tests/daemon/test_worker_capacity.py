"""Worker-capacity behavior at protected provider boundaries."""

import socket
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from threading import Event

from sidekick_usages.core.accounts.identifiers import new_operation_id
from sidekick_usages.core.accounts.types import AuthorityGeneration
from sidekick_usages.core.selection.models import (
    FinalizedSelection,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import OperationKind, ParticipantId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.runtime.scheduler import (
    DurableScheduler,
    ProviderOperationExchangePreparer,
)
from sidekick_usages.daemon.selection.worker import SelectionWorkerGateway
from sidekick_usages.daemon.worker.exchange import WorkerExchangeRegistry
from sidekick_usages.daemon.worker.pool import WorkerPool
from sidekick_usages.persistence.supervisor.results import WorkerResultStore
from sidekick_usages.platform.models import ProcessIdentity
from sidekick_usages.providers.claude.structured.data_plane import (
    ClaudeParticipantChannelRegistry,
    ClaudeProtectedCommitRelay,
)
from tests.fakes.daemon.foundation import foundation_state
from tests.fakes.daemon.runtime import (
    FakeWorkerLauncher,
    RuntimeClock,
    worker_planner,
)

_PARTICIPANTS = (
    ParticipantId("521d4f0d-f92a-4d67-a5fa-f5ec86131337"),
    ParticipantId("b3348405-3d31-410c-9afc-9af6761976dc"),
)


def _protected_channels() -> tuple[
    list[tuple[ParticipantId, int]],
    ClaudeParticipantChannelRegistry,
    list[socket.socket],
]:
    failed: list[tuple[ParticipantId, int]] = []
    channels = ClaudeParticipantChannelRegistry(
        lambda _account_id: True,
        lambda participant_id, generation: failed.append(
            (participant_id, generation)
        ),
    )
    hosts: list[socket.socket] = []
    for index, participant_id in enumerate(_PARTICIPANTS, start=1):
        host, endpoint = socket.socketpair(socket.AF_UNIX)
        transaction = channels.stage(
            participant_id,
            1,
            ProcessIdentity(2000 + index, index),
            endpoint,
        )
        transaction.commit()
        transaction.finalize()
        host.setblocking(False)
        hosts.append(host)
    return failed, channels, hosts


def test_scheduler_prepares_exact_claude_bind_at_available_capacity(
    tmp_path: Path,
) -> None:
    """Prepare each exact Claude bind only when its worker can launch."""
    capacity = foundation_state(tmp_path)
    for queued in capacity.operations:
        capacity.queue.remove(queued.operation_id, expected_state=queued.state)
    blocker_id, *bind_ids = (
        new_operation_id() for _index in range(len(_PARTICIPANTS) + 1)
    )
    clock = RuntimeClock()
    results = WorkerResultStore(capacity.paths.durable_operations)
    releases = {
        operation_id: Event() for operation_id in (blocker_id, *bind_ids)
    }
    blocker = replace(
        capacity.operations[0],
        operation_id=blocker_id,
        kind=OperationKind.MAINTAIN,
    )
    finalized = FinalizedSelection(
        provider_id=ProviderId.CLAUDE,
        account_id=next(iter(capacity.accounts)).account_id,
        epoch=SelectionEpoch(7),
        generation=AuthorityGeneration("finalized-generation"),
        finalized_at=clock.now(),
    )
    exchanges = WorkerExchangeRegistry(clock.monotonic)
    failed, channels, hosts = _protected_channels()
    gateway = SelectionWorkerGateway(
        capacity.queue,
        clock,
        lambda: None,
        exchange_owner=ClaudeProtectedCommitRelay(exchanges, channels),
        operation_id_factory=iter(bind_ids).__next__,
    )
    launcher = FakeWorkerLauncher(
        results,
        clock,
        frozenset(),
        natural_completions=releases,
    )
    workers = WorkerPool(
        launcher,
        worker_planner(),
        lambda: None,
        exchanges=exchanges,
        general_limit=1,
        monotonic=clock.monotonic,
    )
    preparer = ProviderOperationExchangePreparer({ProviderId.CLAUDE: gateway})
    scheduler = DurableScheduler(
        capacity.queue,
        results,
        workers,
        clock,
        exchange_preparer=preparer,
        monotonic=clock.monotonic,
    )
    capacity.queue.enqueue(blocker)
    assert scheduler.dispatch_due()[0].operation_id == blocker_id
    for participant_id in _PARTICIPANTS:
        gateway.bind_finalized(finalized, participant_id, 1)
    expected_bind_ids = tuple(
        operation.operation_id
        for operation in capacity.queue.load()
        if operation.kind is OperationKind.CLAUDE_PARTICIPANT_BIND
    )
    assert len(expected_bind_ids) == len(_PARTICIPANTS)
    assert scheduler.dispatch_due() == ()
    assert not any(
        exchanges.available(operation_id) for operation_id in expected_bind_ids
    )
    for index, operation_id in enumerate(expected_bind_ids):
        releases[blocker_id].set()
        scheduler.collect()
        (started,) = scheduler.dispatch_due()
        assert started.operation_id == operation_id
        assert launcher.specs[-1].exchange_descriptor is not None
        if index == 0:
            assert not exchanges.available(expected_bind_ids[1])
        gateway.abort_exchange(operation_id)
        closed = 0
        for host in hosts:
            with suppress(BlockingIOError):
                closed += host.recv(1) == b""
        assert closed == index + 1
        releases[operation_id].set()
    scheduler.collect()
    assert set(failed) == {
        (participant_id, 1) for participant_id in _PARTICIPANTS
    }
    gateway.close()
    channels.close()
    for host in hosts:
        host.close()

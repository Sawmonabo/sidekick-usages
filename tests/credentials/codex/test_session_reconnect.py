"""Codex participant continuity across supervisor replacement."""

from pathlib import Path

import pytest

from sidekick_usages.cli.session.codex import CodexSessionRuntime
from tests.fakes.codex.app_server.daemon import FakeCodexDaemon
from tests.fakes.codex.app_server.executable import (
    configure_codex_daemon_lifecycle,
)
from tests.fakes.codex.broker.runtime import (
    activation_source_fixture,
    real_worker_executable,
)
from tests.fakes.codex.broker.supervisor import FakeCodexSupervisor
from tests.support.platform import REQUIRES_MANAGED_RUNTIME

pytestmark = REQUIRES_MANAGED_RUNTIME
_ACTIVE_TURN_REQUEST_ID = 2
_QUEUED_TURN_REQUEST_ID = 3
_RECONNECTED_TURN_COUNT = 2


def test_session_reattaches_active_turn_and_releases_queued_turn(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supervisor replacement preserves one resident Codex session."""
    fixture = activation_source_fixture(
        tmp_path,
        short_socket_root,
        monkeypatch,
    )
    paths = fixture.paths
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
        session = CodexSessionRuntime.create(
            fixture.executable,
            fixture.session_home,
            short_socket_root / "reconnecting-participant.sock",
            paths.supervisor_socket,
            environment=fixture.environment,
        )
        with FakeCodexSupervisor(
            paths,
            fixture.executable,
            fixture.session_home,
            fixture.environment,
            real_worker_executable(),
        ) as supervisor:
            supervisor.wait_until_ready()
            session.open()
            tui = daemon.connect_tui(session.socket_path)
            tui.send_request(
                1,
                "thread/resume",
                {"threadId": "thread-reconnect"},
            )
            assert tui.receive().get("id") == 1
            daemon.pause_next_turn()
            tui.send_request(
                _ACTIVE_TURN_REQUEST_ID,
                "turn/start",
                {"input": [], "threadId": "thread-reconnect"},
            )
            daemon.wait_for_paused_turn()
            starts_before = daemon.relay_start_request_ids

        tui.send_request(
            _QUEUED_TURN_REQUEST_ID,
            "turn/start",
            {"input": [], "threadId": "thread-reconnect"},
        )
        assert daemon.relay_start_request_ids == starts_before

        with FakeCodexSupervisor(
            paths,
            fixture.executable,
            fixture.session_home,
            fixture.environment,
            real_worker_executable(),
        ) as restarted:
            restarted.wait_until_ready()
            restarted.wait_for_codex_participants(1, 2)
            daemon.resume_turn()
            responses: set[int] = set()
            completions = 0
            while (
                len(responses) < _RECONNECTED_TURN_COUNT
                or completions < _RECONNECTED_TURN_COUNT
            ):
                message = tui.receive()
                request_id = message.get("id")
                if request_id in {
                    _ACTIVE_TURN_REQUEST_ID,
                    _QUEUED_TURN_REQUEST_ID,
                }:
                    assert isinstance(request_id, int)
                    responses.add(request_id)
                if message.get("method") == "turn/completed":
                    completions += 1
            assert daemon.relay_start_request_ids == (
                *starts_before,
                _QUEUED_TURN_REQUEST_ID,
            )
            restarted.wait_for_codex_participants(1, 0)

        tui.close()
        session.close()

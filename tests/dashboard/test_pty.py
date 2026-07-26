"""Unix pseudoterminal acceptance proof for the interactive dashboard."""

import os
import re
import shlex
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sidekick_usages.cli.dashboard.application import (
    InteractiveDashboardApplication,
)
from sidekick_usages.cli.dashboard.session import (
    InteractiveDashboardSession,
)
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.protocol import ControlEvent
from sidekick_usages.daemon.types.lifecycle import ServiceLifecycleState
from sidekick_usages.persistence.setup.store import (
    ServiceSetupAcknowledgementStore,
)
from sidekick_usages.usage.lookup.worker.client import (
    UsageLookupModuleLaunchPlanner,
    UsageLookupWorkerClient,
)
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupEventObserver,
    UsageLookupWorkerResult,
)
from tests.fakes.dashboard.render import (
    CLAUDE_WARNING_ID,
    interactive_dashboard_state,
)
from tests.fakes.dashboard.runtime import SetupDaemon
from tests.fakes.dashboard.session.control import (
    SessionControlClient,
    SessionControlConnector,
)
from tests.fakes.dashboard.session.models import SESSION_SOCKET
from tests.fakes.dashboard.session.snapshots import (
    SessionSnapshotSource,
    unavailable_session_snapshot,
)
from tests.support.pty import PtySession

ANSI_CONTROL_PATTERN = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[()][0-2A-Z])")
CHILD_MODE_ENVIRONMENT_KEY = "SIDEKICK_PTY_CHILD"
LOOKUP_EXECUTABLE_ENVIRONMENT_KEY = "SIDEKICK_PTY_LOOKUP_EXECUTABLE"
SETUP_ACKNOWLEDGEMENT_ENVIRONMENT_KEY = "SIDEKICK_PTY_SETUP_ACKNOWLEDGEMENT"
TRACE_ENVIRONMENT_KEY = "SIDEKICK_PTY_TRACE"
CHILD_MODE_VALUE = "1"
CHILD_MODULE = "tests.dashboard.test_pty"
CHILD_TIMEOUT_SECONDS = 5.0
INTERRUPTED_EXIT_CODE = 130
FILE_POLL_SECONDS = 0.01
LOOKUP_TIMEOUT_SECONDS = 30.0
LOOKUP_TERMINATION_GRACE_SECONDS = 0.2
PROCESS_ABSENCE_TIMEOUT_SECONDS = 2.0
REFERENCE_TIME = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
WIDE_COLUMNS = 100
NARROW_COLUMNS = 52
TERMINAL_ROWS = 60
UP_KEY = b"\x1b[A"
DOWN_KEY = b"\x1b[B"
ENTER_KEY = b"\r"
APPROVE_KEY = b"y"
REFRESH_KEY = b"r"
TAB_KEY = b"\t"
ESCAPE_KEY = b"\x1b"
HELP_KEY = b"?"
QUIT_KEY = b"q"
INTERRUPT_KEY = b"\x03"
CURSOR_GLYPH = "\N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}"
WIDE_PANEL_TEXT = "CLAUDE · 2 accounts"
NARROW_ACCOUNT_TEXT = "[claude · max]"
SETUP_CONFIRMATION_TEXT = "Sidekick needs one per-user service"
KEY_FOOTER_TEXT = "↑/↓ or j/k move"
HELP_FOOTER_TEXT = "? close help"
STARTUP_FAILURE_TEXT = "cached selection remains"
ACTIVE_LABEL = "work@example.test"
PREVIEW_LABEL = "personal@example.test"
CODEX_EXTERNAL_LABEL = "External Codex CLI login"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class TracingLookupWorker:
    """Record lifecycle outcomes around the real bounded worker client."""

    def __init__(self, worker: UsageLookupWorkerClient) -> None:
        self._worker = worker
        self.cancel_calls = 0
        self.result: UsageLookupWorkerResult | None = None

    def run(
        self,
        observe: UsageLookupEventObserver | None = None,
    ) -> UsageLookupWorkerResult:
        """Run the isolated lookup process and retain its terminal result."""
        self.result = self._worker.run(observe)
        return self.result

    def cancel(self) -> None:
        """Record and forward one bounded cancellation request."""
        self.cancel_calls += 1
        self._worker.cancel()


class TracingSessionControlConnector(SessionControlConnector):
    """Record account refreshes in addition to existing fake activations."""

    def __init__(
        self,
        daemon: SetupDaemon,
        snapshots: SessionSnapshotSource,
    ) -> None:
        super().__init__(daemon, snapshots)
        self.refreshes: list[tuple[ProviderId, SidekickAccountId]] = []

    def __call__(self, socket_path: Path) -> SessionControlClient:
        """Return one tracing client only after synthetic readiness."""
        del socket_path
        if self.daemon.state is not ServiceLifecycleState.READY:
            raise ConnectionRefusedError
        return TracingSessionControlClient(self)


class TracingSessionControlClient(SessionControlClient):
    """Retain refresh intent before yielding the strict fake event stream."""

    def __init__(self, owner: TracingSessionControlConnector) -> None:
        super().__init__(owner)
        self._tracing_owner = owner

    def refresh_account(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
    ) -> Iterator[ControlEvent]:
        """Record one refresh without changing the inherited event contract."""
        self._tracing_owner.refreshes.append((provider_id, account_id))
        return super().refresh_account(provider_id, account_id)


def _child_main() -> int:
    snapshot, _cursor, _footer = interactive_dashboard_state(REFERENCE_TIME)
    unavailable = unavailable_session_snapshot(snapshot)
    snapshots = SessionSnapshotSource(unavailable)
    daemon = SetupDaemon(ServiceLifecycleState.ABSENT)
    connector = TracingSessionControlConnector(daemon, snapshots)
    lookup = TracingLookupWorker(
        UsageLookupWorkerClient(
            UsageLookupModuleLaunchPlanner(
                Path(os.environ[LOOKUP_EXECUTABLE_ENVIRONMENT_KEY]),
                os.environ,
            ),
            timeout_seconds=LOOKUP_TIMEOUT_SECONDS,
            termination_grace_seconds=LOOKUP_TERMINATION_GRACE_SECONDS,
        )
    )
    session = InteractiveDashboardSession(
        unavailable,
        snapshots=snapshots,
        only=None,
        lookup=lookup,
        connector=connector,
        socket_path=SESSION_SOCKET,
        setup=GuidedServiceSetup(
            daemon,
            ServiceSetupAcknowledgementStore(
                Path(os.environ[SETUP_ACKNOWLEDGEMENT_ENVIRONMENT_KEY])
            ),
        ),
        environment={},
    )
    exit_code = InteractiveDashboardApplication(session).run()
    _write_trace(
        Path(os.environ[TRACE_ENVIRONMENT_KEY]),
        exit_code,
        lookup,
        connector,
        daemon,
        snapshots,
    )
    return exit_code


def _write_trace(
    path: Path,
    exit_code: int,
    lookup: TracingLookupWorker,
    connector: TracingSessionControlConnector,
    daemon: SetupDaemon,
    snapshots: SessionSnapshotSource,
) -> None:
    lookup_failure = (
        "none"
        if lookup.result is None or lookup.result.failure is None
        else lookup.result.failure.value
    )
    trace = [
        f"exit={exit_code}",
        f"lookup_cancel_calls={lookup.cancel_calls}",
        f"lookup_failure={lookup_failure}",
        f"daemon_cancelled={str(daemon.cancelled).lower()}",
        f"closed_clients={connector.closed_clients}",
        f"snapshot_loads={snapshots.loads}",
    ]
    trace.extend(f"setup={event}" for event in daemon.events)
    trace.extend(
        f"activation={provider_id.value}:{account_id}:{str(approved).lower()}"
        for provider_id, account_id, approved in connector.activations
    )
    trace.extend(
        f"refresh={provider_id.value}:{account_id}"
        for provider_id, account_id in connector.refreshes
    )
    path.write_text("\n".join(trace) + "\n", encoding="utf-8")


def _isolated_environment(
    root: Path,
    lookup_executable: Path,
    trace_path: Path,
) -> dict[str, str]:
    home = root / "home"
    config = root / "config"
    data = root / "data"
    runtime = root / "runtime"
    temporary = root / "tmp"
    for path in (home, config, data, runtime, temporary):
        path.mkdir(mode=0o700, parents=True)
    return {
        "HOME": str(home),
        "LANG": "C",
        "PATH": os.defpath,
        "PYTHONUTF8": "1",
        "TERM": "xterm-256color",
        "TMPDIR": str(temporary),
        "XDG_CONFIG_HOME": str(config),
        "XDG_DATA_HOME": str(data),
        "XDG_RUNTIME_DIR": str(runtime),
        CHILD_MODE_ENVIRONMENT_KEY: CHILD_MODE_VALUE,
        LOOKUP_EXECUTABLE_ENVIRONMENT_KEY: str(lookup_executable),
        SETUP_ACKNOWLEDGEMENT_ENVIRONMENT_KEY: str(
            data / "service-setup-acknowledgement.json"
        ),
        TRACE_ENVIRONMENT_KEY: str(trace_path),
    }


def _write_blocking_lookup_executable(
    path: Path,
    process_id_path: Path,
) -> None:
    quoted_process_id_path = shlex.quote(str(process_id_path))
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$$\" > {quoted_process_id_path}\n"
        "trap 'exit 0' HUP INT TERM\n"
        "while :; do\n"
        "    sleep 60\n"
        "done\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def _start_dashboard(
    root: Path,
) -> tuple[PtySession, Path, Path]:
    lookup_executable = root / "synthetic-lookup"
    lookup_process_id = root / "lookup.pid"
    trace_path = root / "trace.txt"
    _write_blocking_lookup_executable(
        lookup_executable,
        lookup_process_id,
    )
    environment = _isolated_environment(
        root,
        lookup_executable,
        trace_path,
    )
    session = PtySession.start(
        (sys.executable, "-m", CHILD_MODULE),
        cwd=REPOSITORY_ROOT,
        environment=environment,
        columns=WIDE_COLUMNS,
        rows=TERMINAL_ROWS,
    )
    return session, lookup_process_id, trace_path


@contextmanager
def _dashboard_process(
    root: Path,
) -> Iterator[tuple[PtySession, Path, Path]]:
    session, lookup_process_id, trace_path = _start_dashboard(root)
    try:
        with session:
            yield session, lookup_process_id, trace_path
    finally:
        _force_lookup_cleanup(lookup_process_id)


def _read_process_id(path: Path) -> int:
    deadline = time.monotonic() + CHILD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except FileNotFoundError, ValueError:
            time.sleep(FILE_POLL_SECONDS)
    raise AssertionError("Synthetic lookup process did not start.")


def _force_lookup_cleanup(path: Path) -> None:
    try:
        process_id = int(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError, ValueError:
        return
    try:
        os.killpg(process_id, signal.SIGKILL)
    except ProcessLookupError:
        return


def _wait_for_process_absence(process_id: int) -> None:
    deadline = time.monotonic() + PROCESS_ABSENCE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return
        time.sleep(FILE_POLL_SECONDS)
    raise AssertionError("Synthetic lookup process was not reaped.")


def _plain_terminal_output(output: str) -> str:
    return ANSI_CONTROL_PATTERN.sub("", output).replace("\r", "")


def _selected(output: str, label: str) -> bool:
    plain = _plain_terminal_output(output)
    pattern = rf"{CURSOR_GLYPH}\s+●\s+{re.escape(label)}"
    return re.search(pattern, plain) is not None


def _trace_lines(path: Path) -> tuple[str, ...]:
    return tuple(path.read_text(encoding="utf-8").splitlines())


def _resize_and_read(session: PtySession, columns: int) -> str:
    session.clear_output()
    session.resize(columns, TERMINAL_ROWS)
    return session.read_until(KEY_FOOTER_TEXT)


def _send_resize_and_read(
    session: PtySession,
    key: bytes,
    columns: int,
) -> str:
    _send_key(session, key)
    return _resize_and_read(session, columns)


def _send_key(session: PtySession, key: bytes) -> None:
    session.clear_output()
    session.send(key)
    session.read_until(CURSOR_GLYPH)


def test_dashboard_pty_completes_the_interactive_account_journey(
    tmp_path: Path,
) -> None:
    with _dashboard_process(tmp_path) as (
        session,
        lookup_process_id_path,
        trace_path,
    ):
        initial = session.read_until(STARTUP_FAILURE_TEXT)
        lookup_process_id = _read_process_id(lookup_process_id_path)
        plain_initial = _plain_terminal_output(initial)
        assert (
            WIDE_PANEL_TEXT in plain_initial,
            ACTIVE_LABEL in plain_initial,
            PREVIEW_LABEL in plain_initial,
            _selected(initial, ACTIVE_LABEL),
        ) == (True, True, True, True)

        moved_down = _send_resize_and_read(
            session,
            DOWN_KEY,
            WIDE_COLUMNS + 1,
        )
        assert WIDE_PANEL_TEXT in _plain_terminal_output(moved_down)
        assert _selected(moved_down, PREVIEW_LABEL)

        moved_up = _send_resize_and_read(
            session,
            UP_KEY,
            WIDE_COLUMNS + 2,
        )
        assert WIDE_PANEL_TEXT in _plain_terminal_output(moved_up)
        assert _selected(moved_up, ACTIVE_LABEL)

        _send_key(session, DOWN_KEY)
        restored = _send_resize_and_read(
            session,
            ESCAPE_KEY,
            WIDE_COLUMNS + 3,
        )
        assert _selected(restored, ACTIVE_LABEL)

        _send_key(session, DOWN_KEY)
        session.send(ENTER_KEY)
        session.read_until(SETUP_CONFIRMATION_TEXT)
        session.clear_output()
        session.send(APPROVE_KEY)
        session.read_until(KEY_FOOTER_TEXT)

        session.clear_output()
        session.send(REFRESH_KEY)
        session.read_until(KEY_FOOTER_TEXT)

        codex = _send_resize_and_read(
            session,
            TAB_KEY,
            WIDE_COLUMNS + 4,
        )
        assert _selected(codex, CODEX_EXTERNAL_LABEL)

        narrow = _resize_and_read(session, NARROW_COLUMNS)
        assert NARROW_ACCOUNT_TEXT in _plain_terminal_output(narrow)
        assert WIDE_PANEL_TEXT not in _plain_terminal_output(narrow)

        wide = _resize_and_read(session, WIDE_COLUMNS)
        assert WIDE_PANEL_TEXT in _plain_terminal_output(wide)
        assert NARROW_ACCOUNT_TEXT not in _plain_terminal_output(wide)

        session.clear_output()
        session.send(HELP_KEY)
        session.read_until(HELP_FOOTER_TEXT)
        session.send(QUIT_KEY)
        assert session.wait() == 0
        assert (
            session.terminal_restored,
            session.echo_enabled,
            session.canonical_mode_enabled,
        ) == (True, True, True)
        assert session.wait_for_process_group_exit()
    _wait_for_process_absence(lookup_process_id)
    trace = _trace_lines(trace_path)
    assert "exit=0" in trace
    assert "lookup_cancel_calls=1" in trace
    assert "lookup_failure=canceled" in trace
    assert "daemon_cancelled=true" in trace
    assert "setup=status:claude" in trace
    assert "setup=install:claude" in trace
    assert f"activation=claude:{CLAUDE_WARNING_ID}:false" in trace
    assert f"refresh=claude:{CLAUDE_WARNING_ID}" in trace


def test_dashboard_pty_interrupt_restores_terminal_and_reaps_lookup(
    tmp_path: Path,
) -> None:
    with _dashboard_process(tmp_path) as (
        session,
        lookup_process_id_path,
        trace_path,
    ):
        session.read_until(STARTUP_FAILURE_TEXT)
        lookup_process_id = _read_process_id(lookup_process_id_path)
        session.send(INTERRUPT_KEY)
        assert session.wait() == INTERRUPTED_EXIT_CODE
        assert (
            session.terminal_restored,
            session.echo_enabled,
            session.canonical_mode_enabled,
        ) == (True, True, True)
        assert session.wait_for_process_group_exit()
    _wait_for_process_absence(lookup_process_id)
    trace = _trace_lines(trace_path)
    assert "exit=130" in trace
    assert "lookup_cancel_calls=1" in trace
    assert "lookup_failure=canceled" in trace
    assert "daemon_cancelled=true" in trace
    assert "closed_clients=0" in trace


if (
    __name__ == "__main__"
    and os.environ.get(CHILD_MODE_ENVIRONMENT_KEY) == CHILD_MODE_VALUE
):
    sys.exit(_child_main())

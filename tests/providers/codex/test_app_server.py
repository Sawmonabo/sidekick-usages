"""Load-bearing tests for the versioned Codex app-server boundary."""

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Event, RLock, Thread

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import SelectionEpoch
from sidekick_usages.core.selection.types import SelectionCode, TurnId
from sidekick_usages.providers.codex.app_server import (
    capabilities,
    errors,
    executable,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.codec import (
    decode_json_rpc_routing,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.models import (
    JsonRpcNotification,
)
from sidekick_usages.providers.codex.app_server.models import CodexExecutable
from sidekick_usages.providers.codex.app_server.session import (
    CodexAppServerSession,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.broker.daemon import CodexDaemonManager
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from sidekick_usages.providers.codex.session.models import (
    CODEX_SESSION_OPERATOR_PRECONDITION,
    CodexLoadedThreadSnapshot,
    CodexRelayAdmission,
    CodexRelayAdmissionState,
    CodexRelayAuthority,
    CodexSessionCapability,
    CodexSessionConfigurationReason,
)
from sidekick_usages.providers.codex.session.relay import (
    CodexAdmissionRelay,
    CodexRelayError,
)
from sidekick_usages.serialization.json import JsonObject
from tests.fakes.codex.app_server.daemon import (
    FakeCodexDaemon,
    FakeCodexTuiObserver,
)
from tests.fakes.codex.app_server.executable import (
    RAW_PROVIDER_SECRET,
    configure_codex_daemon_lifecycle,
    write_fake_codex,
    write_fake_managed_codex,
)
from tests.fakes.codex.app_server.model import SyntheticCodexModelAttempt
from tests.fakes.codex.app_server.schema import write_codex_schema
from tests.fakes.codex.auth import managed_auth
from tests.support.platform import REQUIRES_MANAGED_RUNTIME

pytestmark = REQUIRES_MANAGED_RUNTIME

SCHEMA_HASH_HEX_LENGTH = 64
_NEWER_CODEX_VERSION = "0.146.0"
_ACCOUNT_ID = SidekickAccountId("33333333-3333-4333-8333-333333333333")
_PROVIDER_IDENTITY = "workspace-account-alpha"
_GENERATION = "2026-07-24T10:00:00.000000000Z"
_NATIVE_AUTH = managed_auth(_PROVIDER_IDENTITY, _GENERATION)
_SESSION_PROVIDER = "sidekick-chatgpt-http"
_SESSION_BASE_URL = "https://chatgpt.com/backend-api/codex"
_NO_LOADED_THREADS = CodexLoadedThreadSnapshot(revision=0, thread_ids=())
_SESSION_SCHEMA_MANIFEST = (
    "v2/ConfigReadParams.json",
    "v2/ConfigReadResponse.json",
    "v2/ModelProviderCapabilitiesReadParams.json",
    "v2/ModelProviderCapabilitiesReadResponse.json",
    "v2/TurnStartParams.json",
    "v2/TurnStartResponse.json",
    "v2/TurnStartedNotification.json",
    "v2/TurnCompletedNotification.json",
    "v2/ThreadRealtimeStartParams.json",
    "v2/ThreadRealtimeStartResponse.json",
    "v2/ThreadRealtimeStartedNotification.json",
    "v2/ThreadRealtimeClosedNotification.json",
    "v2/ListMcpServerStatusParams.json",
    "v2/ListMcpServerStatusResponse.json",
    "v2/McpServerStatusUpdatedNotification.json",
)
_ROUTING_REQUEST_ID = 7
_RELAY_WAIT_SECONDS = 5.0
_LOADED_THREAD_BOUND = 256
_BASELINE_AUTHORITY = CodexRelayAuthority(
    account_id=_ACCOUNT_ID,
    generation=AuthorityGeneration(_GENERATION),
    epoch=SelectionEpoch(1),
)
_TARGET_AUTHORITY = CodexRelayAuthority(
    account_id=SidekickAccountId("44444444-4444-4444-8444-444444444444"),
    generation=AuthorityGeneration("2026-07-24T11:00:00.000000000Z"),
    epoch=SelectionEpoch(2),
)


@dataclass(frozen=True, slots=True)
class _SessionCase:
    version: str = "0.146.0"
    model_provider: str | None = None
    base_url: str | None = None
    requires_openai_auth: bool | None = None
    supports_websockets: bool | None = None
    user_config: JsonObject | None = None
    project_config: JsonObject | None = None
    configuration_required: bool = False
    protocol_unsupported: bool = False
    supported: bool = False


class _RelayControl:
    def __init__(self, authority: CodexRelayAuthority) -> None:
        self._condition = Condition(RLock())
        self._authority = authority
        self._open = True
        self._pause_states: set[CodexRelayAdmissionState] = set()
        self._block_adoption = False
        self._fail_end = False
        self.events = {
            name: Event()
            for name in (
                "queued-started",
                "queued-resume",
                "admitted-started",
                "admitted-resume",
                "adoption-started",
                "adoption-resume",
            )
        }
        self.begun: list[TurnId] = []
        self.ended: list[TurnId] = []
        self.ready_targets: list[CodexRelayAuthority] = []
        self.adoptions: list[tuple[TurnId, CodexRelayAuthority]] = []
        self.mcp_statuses: list[JsonObject] = []

    def request(self, method: str, params: JsonObject) -> JsonObject:
        """Return an exact zero-MCP response for one loaded thread."""
        assert method == "mcpServerStatus/list"
        assert set(params) == {"threadId"}
        return self.mcp_statuses.pop(0) if self.mcp_statuses else {"data": []}

    def close_gate(self) -> None:
        with self._condition:
            self._open = False

    def open_gate(self, authority: CodexRelayAuthority) -> None:
        with self._condition:
            self._authority = authority
            self._open = True

    def fail_next_end(self) -> None:
        self._fail_end = True

    def arm_open_race(self) -> None:
        with self._condition:
            self._pause_states = set(CodexRelayAdmissionState)
            self._block_adoption = True
        for event in self.events.values():
            event.clear()

    def wait_count(
        self,
        values: list[TurnId] | list[tuple[TurnId, CodexRelayAuthority]],
        count: int,
        timeout: float = _RELAY_WAIT_SECONDS,
    ) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: len(values) >= count,
                timeout=timeout,
            )

    def begin(self, turn_id: TurnId) -> CodexRelayAdmission:
        with self._condition:
            state = (
                CodexRelayAdmissionState.ADMITTED
                if self._open
                else CodexRelayAdmissionState.QUEUED
            )
            authority = self._authority if self._open else None
            pause = state in self._pause_states
            self._pause_states.discard(state)
            started = self.events[f"{state.value}-started"]
            resumed = self.events[f"{state.value}-resume"]
            self.begun.append(turn_id)
            self._condition.notify_all()
        if pause:
            started.set()
            self._wait_event(resumed)
        return CodexRelayAdmission(
            turn_id=turn_id,
            state=state,
            authority=authority,
        )

    def recheck(self, admission: CodexRelayAdmission) -> None:
        with self._condition:
            if not self._open or admission.authority != self._authority:
                raise AssertionError("Relay admission was not rechecked.")

    def end(self, turn_id: TurnId) -> None:
        with self._condition:
            self.ended.append(turn_id)
            self._condition.notify_all()
            if self._fail_end:
                self._fail_end = False
                raise CodexRelayError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )

    def ready(self, target: CodexRelayAuthority) -> None:
        with self._condition:
            self.ready_targets.append(target)
            self._condition.notify_all()

    def adopted(
        self,
        turn_id: TurnId,
        target: CodexRelayAuthority,
    ) -> None:
        with self._condition:
            block = self._block_adoption
            self._block_adoption = False
            self.adoptions.append((turn_id, target))
            self._condition.notify_all()
        if block:
            self.events["adoption-started"].set()
            self._wait_event(self.events["adoption-resume"])

    @staticmethod
    def _wait_event(event: Event) -> None:
        if not event.wait(_RELAY_WAIT_SECONDS):
            raise AssertionError("Timed out at a relay race boundary.")


def _receive_response(
    tui: FakeCodexTuiObserver,
    request_id: int,
) -> JsonObject:
    for _message_index in range(16):
        message = tui.receive()
        if message.get("id") == request_id:
            return message
    raise AssertionError("Fake Codex TUI saw no correlated response.")


def _receive_method(tui: FakeCodexTuiObserver, method: str) -> None:
    for _message_index in range(16):
        if tui.receive().get("method") == method:
            return
    raise AssertionError("Fake Codex TUI saw no provider terminal event.")


def _send_turn(
    tui: FakeCodexTuiObserver,
    request_id: int,
    thread_id: str,
) -> None:
    tui.send_request(
        request_id,
        "turn/start",
        {"input": [], "threadId": thread_id},
    )


def _resume_thread(
    tui: FakeCodexTuiObserver,
    request_id: int,
    thread_id: str,
) -> None:
    tui.send_request(request_id, "thread/resume", {"threadId": thread_id})
    _receive_response(tui, request_id)


def _prove_relay_routing_codec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prove only relay-owned identifiers and raw bytes are observed."""
    raw_turn = (
        b'{"id":7,"method":"turn/start","params":'
        b'{"input":[{"text":"provider-content"}],'
        b'"threadId":"thread-alpha"}}'
    )

    def reject_recursive_decode(_payload: bytes) -> JsonObject:
        raise AssertionError("Relay routing decoded provider content.")

    with monkeypatch.context() as routing_context:
        routing_context.setattr(
            "sidekick_usages.providers.codex.app_server.jsonrpc.codec."
            "decode_json_object",
            reject_recursive_decode,
        )
        routing = decode_json_rpc_routing(raw_turn, from_client=True)
    assert (
        routing.request_id,
        routing.method,
        routing.thread_id,
        routing.turn_id,
        routing.raw,
    ) == (
        _ROUTING_REQUEST_ID,
        "turn/start",
        "thread-alpha",
        None,
        raw_turn,
    )


def _prove_relay_baseline(
    relay: CodexAdmissionRelay,
    tui: FakeCodexTuiObserver,
    control: _RelayControl,
    daemon: FakeCodexDaemon,
) -> None:
    """Prove natural terminals, bounded FIFO, and baseline reopen."""
    tui.send_request(
        10,
        "turn/start",
        {
            "input": [{"text": "provider-content"}],
            "threadId": "thread-turn-a",
        },
    )
    assert _receive_response(tui, 10).get("result") == {
        "turn": {"id": "turn-10"}
    }
    _receive_method(tui, "turn/completed")
    tui.send_request(
        11,
        "thread/realtime/start",
        {"threadId": "thread-realtime-a"},
    )
    assert _receive_response(tui, 11).get("result") == {}
    _receive_method(tui, "thread/realtime/closed")
    assert control.wait_count(control.ended, 2)

    control.close_gate()
    baseline_ids = tuple(range(20, 36))
    for request_id in baseline_ids:
        _send_turn(tui, request_id, f"thread-baseline-{request_id}")
    assert control.wait_count(control.begun, 18)
    _send_turn(tui, 36, "thread-refused")
    backpressure = _receive_response(tui, 36).get("error")
    assert isinstance(backpressure, dict)
    assert backpressure.get("data") == {
        "code": SelectionCode.ACTIVE_OPERATION_TIMEOUT.value
    }

    control.open_gate(_BASELINE_AUTHORITY)
    relay.reopen_baseline(_BASELINE_AUTHORITY)
    for request_id in baseline_ids:
        _receive_response(tui, request_id)
    assert control.wait_count(control.ended, 18)
    assert daemon.relay_start_request_ids == (10, 11, *baseline_ids)
    assert control.ready_targets == []
    assert control.adoptions == []

    control.fail_next_end()
    _send_turn(tui, 37, "thread-turn-a")
    _receive_response(tui, 37)
    _receive_method(tui, "turn/completed")
    tui.send_request(38, "account/read", {"refreshToken": False})
    assert "result" in _receive_response(tui, 38)
    _send_turn(tui, 39, "thread-degraded")
    refusal = _receive_response(tui, 39).get("error")
    assert isinstance(refusal, dict)
    assert refusal.get("data") == {
        "code": SelectionCode.SELECTION_RECOVERY_REQUIRED.value
    }
    assert control.wait_count(control.ended, 19)
    relay.prepare_admission()


def _prove_relay_target(
    relay: CodexAdmissionRelay,
    tui: FakeCodexTuiObserver,
    control: _RelayControl,
    daemon: FakeCodexDaemon,
) -> None:
    """Prove versioned readiness, atomic OPEN, and one adoption."""
    control.close_gate()
    _resume_thread(tui, 60, "thread-snapshot-a")
    stale_snapshot = relay.loaded_threads_snapshot
    _resume_thread(tui, 61, "thread-snapshot-b")
    with pytest.raises(CodexRelayError) as stale_ready:
        relay.mark_ready(_TARGET_AUTHORITY, stale_snapshot)
    assert stale_ready.value.code is SelectionCode.AUTHORITY_PROOF_FAILED

    control.mcp_statuses.append(
        {"data": [{"name": "synthetic", "status": "ready"}]}
    )
    with pytest.raises(CodexRelayError) as nonempty_mcp:
        relay.mark_ready(_TARGET_AUTHORITY, relay.loaded_threads_snapshot)
    assert (
        nonempty_mcp.value.code is SelectionCode.UNSUPPORTED_SESSION_CAPABILITY
    )
    relay.mark_ready(_TARGET_AUTHORITY, relay.loaded_threads_snapshot)
    _resume_thread(tui, 62, "thread-snapshot-c")
    with pytest.raises(CodexRelayError) as invalidated_open:
        relay.open_admission(_TARGET_AUTHORITY)
    assert (
        invalidated_open.value.code
        is SelectionCode.SELECTION_RECOVERY_REQUIRED
    )
    relay.mark_ready(_TARGET_AUTHORITY, relay.loaded_threads_snapshot)

    control.arm_open_race()
    _send_turn(tui, 70, "thread-race-first")
    _RelayControl._wait_event(control.events["queued-started"])
    open_started, open_finished = Event(), Event()

    def open_target() -> None:
        control.open_gate(_TARGET_AUTHORITY)
        open_started.set()
        relay.open_admission(_TARGET_AUTHORITY)
        open_finished.set()

    open_thread = Thread(target=open_target)
    open_thread.start()
    _RelayControl._wait_event(open_started)
    finished_before_queue_install = open_finished.wait(0.1)
    control.events["queued-resume"].set()
    if finished_before_queue_install:
        open_thread.join(timeout=_RELAY_WAIT_SECONDS)
    assert not finished_before_queue_install

    _RelayControl._wait_event(control.events["admitted-started"])
    _send_turn(tui, 71, "thread-race-second")
    control.events["admitted-resume"].set()
    _RelayControl._wait_event(control.events["adoption-started"])
    duplicate = control.wait_count(control.adoptions, 2, 0.1)
    control.events["adoption-resume"].set()
    open_thread.join(timeout=_RELAY_WAIT_SECONDS)
    assert not open_thread.is_alive()
    assert not duplicate
    _receive_response(tui, 70)
    _receive_response(tui, 71)
    assert control.wait_count(control.ended, 21)
    assert daemon.relay_start_request_ids[-2:] == (70, 71)
    assert control.adoptions == [(control.begun[-2], _TARGET_AUTHORITY)]
    assert control.ready_targets == [_TARGET_AUTHORITY, _TARGET_AUTHORITY]


def _prove_loaded_thread_bound(
    relay: CodexAdmissionRelay,
    tui: FakeCodexTuiObserver,
) -> None:
    """Prove relay state is bounded and fails closed on overflow."""
    for request_id in range(100, 333):
        _resume_thread(tui, request_id, f"thread-bound-{request_id}")
    full_snapshot = relay.loaded_threads_snapshot
    assert len(full_snapshot.thread_ids) == _LOADED_THREAD_BOUND
    tui.send_request(400, "thread/resume", {"threadId": "thread-overflow"})
    tui.wait_closed()
    with pytest.raises(CodexRelayError) as overflow:
        relay.mark_ready(_TARGET_AUTHORITY, full_snapshot)
    assert overflow.value.code is SelectionCode.UNSUPPORTED_SESSION_CAPABILITY


def _prove_versioned_relay_journey(short_socket_root: Path) -> None:
    """Prove the complete provider-local relay state boundary."""
    daemon_home = short_socket_root / "relay-daemon"
    relay_root = short_socket_root / "relay"
    daemon_home.mkdir(mode=0o700)
    relay_root.mkdir(mode=0o700)
    relay_path = relay_root / "participant.sock"
    control = _RelayControl(_BASELINE_AUTHORITY)
    turn_index = 0

    def next_turn_id() -> TurnId:
        nonlocal turn_index
        turn_index += 1
        return TurnId(f"10000000-0000-4000-8000-{turn_index:012d}")

    with FakeCodexDaemon(
        daemon_home,
        app_server_version=_NEWER_CODEX_VERSION,
    ) as daemon:
        upstream_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream_socket.connect(str(daemon.socket_path))
        relay = CodexAdmissionRelay.open(
            relay_path,
            upstream_socket,
            control,
            control,
            control,
            turn_id_factory=next_turn_id,
        )
        relay.seed_baseline(_BASELINE_AUTHORITY)
        try:
            tui = daemon.connect_tui(relay.socket_path)
            try:
                tui.send_request(
                    2,
                    "account/read",
                    {"refreshToken": False},
                )
                result = _receive_response(tui, 2).get("result")
                assert isinstance(result, dict)
                assert result.get("requiresOpenaiAuth") is True
                for request_id, method in enumerate(
                    (
                        "account/login/start",
                        "account/login/cancel",
                        "account/logout",
                    ),
                    start=4,
                ):
                    tui.send_request(request_id, method, {})
                    response = _receive_response(tui, request_id)
                    error = response.get("error")
                    assert isinstance(error, dict)
                    assert error.get("data") == {
                        "code": (
                            SelectionCode.UNCOORDINATED_AUTH_MUTATION.value
                        )
                    }

                _prove_relay_baseline(relay, tui, control, daemon)
                _prove_relay_target(relay, tui, control, daemon)
                _prove_loaded_thread_bound(relay, tui)

            finally:
                tui.close()
        finally:
            relay.close()
            upstream_socket.close()


def _prove_synthetic_current_auth_http(
    capability: CodexSessionCapability,
    daemon: FakeCodexDaemon,
) -> None:
    """Prove only the synthetic model-attempt selection boundary."""
    accounts = (
        ProviderIdentity("synthetic-account-a"),
        ProviderIdentity("synthetic-account-b"),
    )
    model_attempt = SyntheticCodexModelAttempt(
        capability,
        daemon.read_current_external_auth,
    )
    daemon.install_external_auth(accounts[0], "synthetic-generation-a")
    model_attempt.attempt()
    daemon.install_external_auth(accounts[1], "synthetic-generation-b")
    model_attempt.attempt()
    assert daemon.model_auth_read_count == model_attempt.auth_resolutions
    assert model_attempt.auth_resolutions == len(accounts)
    assert model_attempt.http_accounts == accounts
    assert model_attempt.websocket_opens == 0


def _prepare_shared_runtime(
    executable: CodexExecutable,
    native_home: Path,
    environment: dict[str, str],
    expected_user_id: int | None,
) -> CodexSharedRuntime:
    runtime = CodexSharedRuntime.create(
        executable,
        native_home,
        environment=environment,
        expected_user_id=expected_user_id,
        loaded_threads=lambda: _NO_LOADED_THREADS,
    )
    runtime.prepare(
        _ACCOUNT_ID,
        ProviderIdentity(_PROVIDER_IDENTITY),
        AuthorityGeneration(_GENERATION),
    )
    return runtime


def test_versioned_codex_app_server_boundary_is_complete(
    tmp_path: Path,
    short_socket_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_root = tmp_path / "schema"
    write_codex_schema(schema_root, external_auth=True)
    codex_home = short_socket_root / "private-codex-home"
    write_fake_managed_codex(
        tmp_path,
        schema_root,
        codex_home,
        version=_NEWER_CODEX_VERSION,
    )
    executable_path = tmp_path / "codex"
    environment = {
        "HOME": str(tmp_path),
        "OPENAI_API_KEY": RAW_PROVIDER_SECRET,
        "PATH": os.pathsep.join((str(tmp_path), os.environ["PATH"])),
    }

    server = executable.discover_codex_executable(environment)
    support = capabilities.probe_codex_capabilities(server, environment)
    manager = CodexDaemonManager(
        support,
        codex_home,
        environment=environment,
    )
    with FakeCodexDaemon(
        codex_home,
        app_server_version=_NEWER_CODEX_VERSION,
    ) as daemon:
        lifecycle = configure_codex_daemon_lifecycle(
            tmp_path,
            codex_home,
            daemon.socket_path,
            app_server_version=_NEWER_CODEX_VERSION,
            already_running=True,
        )
        authority = manager.attach_running()
        connection = manager.connect(authority)
        manager.revalidate(authority)
        connection.close()
    assert (
        lifecycle.start_statuses,
        lifecycle.restart_count,
        lifecycle.version_count,
    ) == ((), 0, 1)
    with CodexAppServerSession.open(
        support,
        codex_home,
        environment,
    ) as session:
        result = session.request(
            "account/read",
            {"refreshToken": False},
        )
        notification = session.receive()

        assert server.provenance.path == executable_path.resolve()
        assert str(server.version) == _NEWER_CODEX_VERSION
        assert len(support.schema_hash) == SCHEMA_HASH_HEX_LENGTH
        assert support.session_schema_manifest == (_SESSION_SCHEMA_MANIFEST)
        assert result["requiresOpenaiAuth"] is True
        assert isinstance(notification, JsonRpcNotification)
        assert notification.method == "account/updated"

        _prove_relay_routing_codec(monkeypatch)
    assert session.closed

    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert events[0]["argv"] == ["--version"]
    assert events[1]["argv"][:4] == [
        "app-server",
        "generate-json-schema",
        "--experimental",
        "--out",
    ]
    assert not Path(events[1]["argv"][4]).exists()
    assert events[2]["argv"] == ["app-server"]
    assert events[3]["method"] == "getAuthStatus"
    assert all(event.get("openai_api_key") is None for event in events)
    assert events[-1]["codex_home"] == str(codex_home)

    replacement = tmp_path / "replacement-codex"
    replacement.write_bytes(executable_path.read_bytes())
    replacement.chmod(0o700)
    replacement.replace(executable_path)
    with pytest.raises(errors.CodexAppServerError) as changed:
        manager.attach_running()
    assert changed.value.code is CodexAppServerFailure.EXECUTABLE_UNSAFE

    _prove_versioned_relay_journey(short_socket_root)


def test_codex_app_server_boundary_fails_closed_and_redacted(
    tmp_path: Path,
) -> None:
    schema_root = tmp_path / "schema"
    write_codex_schema(schema_root, external_auth=True)
    write_fake_codex(tmp_path, schema_root)
    environment = {
        "HOME": str(tmp_path),
        "OPENAI_API_KEY": RAW_PROVIDER_SECRET,
        "PATH": os.pathsep.join((str(tmp_path), os.environ["PATH"])),
    }
    server = executable.discover_codex_executable(environment)
    support = capabilities.probe_codex_capabilities(server, environment)
    codex_home = tmp_path / "private-codex-home"
    codex_home.mkdir()
    (tmp_path / "mode").write_text("malformed", encoding="utf-8")

    with pytest.raises(errors.CodexAppServerError) as malformed:
        CodexAppServerSession.open(
            support,
            codex_home,
            environment,
        )

    assert malformed.value.code is CodexAppServerFailure.PROTOCOL_MALFORMED
    assert RAW_PROVIDER_SECRET not in repr(malformed.value)
    process_id = int((tmp_path / "app-server.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(process_id, 0)


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _SessionCase(supported=True),
            id="direct-http-current-auth",
        ),
        pytest.param(
            _SessionCase(
                version="0.145.0",
                protocol_unsupported=True,
            ),
            id="wrong-version",
        ),
        pytest.param(
            _SessionCase(
                model_provider="user-provider",
                configuration_required=True,
            ),
            id="overridden-provider",
        ),
        pytest.param(
            _SessionCase(
                base_url="https://example.invalid/codex",
                configuration_required=True,
            ),
            id="wrong-base-url",
        ),
        pytest.param(
            _SessionCase(
                requires_openai_auth=False,
                configuration_required=True,
            ),
            id="missing-openai-auth",
        ),
        pytest.param(
            _SessionCase(
                supports_websockets=True,
                configuration_required=True,
            ),
            id="websockets-enabled",
        ),
        pytest.param(
            _SessionCase(
                user_config={"model_provider": "user-provider"},
                configuration_required=True,
            ),
            id="user-protected-override",
        ),
        pytest.param(
            _SessionCase(
                project_config={
                    "model_providers": {
                        _SESSION_PROVIDER: {"base_url": "project-url"}
                    }
                },
                configuration_required=True,
            ),
            id="project-protected-override",
        ),
    ],
)
def test_neutral_runtime_requires_current_auth_without_model_websockets(
    tmp_path: Path,
    short_socket_root: Path,
    case: _SessionCase,
) -> None:
    schema_root = tmp_path / "schema"
    session_home = short_socket_root / "session"
    session_home.mkdir()
    session_settings = session_home / "config.toml"
    unrelated_settings = b'model = "gpt-test"\n'
    session_settings.write_bytes(unrelated_settings)
    write_codex_schema(schema_root, external_auth=True)
    write_fake_managed_codex(
        tmp_path,
        schema_root,
        session_home,
        version=case.version,
    )
    environment = {
        "HOME": str(tmp_path),
        "PATH": os.pathsep.join((str(tmp_path), os.environ["PATH"])),
    }
    server = executable.discover_codex_executable(environment)

    with FakeCodexDaemon(
        session_home,
        app_server_version=case.version,
        model_provider=case.model_provider,
        base_url=case.base_url,
        requires_openai_auth=case.requires_openai_auth,
        supports_websockets=case.supports_websockets,
        user_config=case.user_config,
        project_config=case.project_config,
    ) as daemon:
        configure_codex_daemon_lifecycle(
            tmp_path,
            session_home,
            daemon.socket_path,
            app_server_version=case.version,
        )
        runtime = CodexSharedRuntime.create(
            server,
            session_home,
            environment=environment,
            loaded_threads=lambda: _NO_LOADED_THREADS,
        )

        if case.protocol_unsupported:
            with pytest.raises(CodexBrokerError) as unsupported:
                runtime.qualify_session_transport()
            assert (
                unsupported.value.code
                is CodexBrokerFailure.PROTOCOL_UNSUPPORTED
            )
            runtime.close()
            return
        if case.configuration_required:
            with pytest.raises(CodexBrokerError) as refused:
                runtime.qualify_session_transport()
            assert (
                refused.value.code
                is CodexBrokerFailure.SESSION_CONFIGURATION_REQUIRED
            )
            report = refused.value.preparation_report
            assert report is not None
            assert report.reason in {
                CodexSessionConfigurationReason.PROTECTED_OVERRIDE,
                CodexSessionConfigurationReason.RESIDENT_CONFIG_STALE,
            }
            assert report.dry_run is True
            assert (
                report.operator_steps[0] == CODEX_SESSION_OPERATOR_PRECONDITION
            )
            assert session_settings.read_bytes() == unrelated_settings
            assert not (runtime.codex_home / "auth.json").exists()
            runtime.close()
            return

        capability = runtime.qualify_session_transport()

        assert capability.model_provider == (
            _SESSION_PROVIDER
            if case.model_provider is None
            else case.model_provider
        )
        assert capability.base_url == (
            _SESSION_BASE_URL if case.base_url is None else case.base_url
        )
        assert capability.requires_openai_auth is (
            True
            if case.requires_openai_auth is None
            else case.requires_openai_auth
        )
        assert capability.supports_websockets is (
            False
            if case.supports_websockets is None
            else case.supports_websockets
        )
        assert capability.supported is case.supported
        if case.supported:
            _prove_synthetic_current_auth_http(capability, daemon)
        assert session_settings.read_bytes() == unrelated_settings
        assert not (runtime.codex_home / "auth.json").exists()
        runtime.close()


def test_shared_codex_runtime_self_heals_and_rejects_unsafe_authority(
    tmp_path: Path,
    short_socket_root: Path,
) -> None:
    cases = (
        (
            "version",
            True,
            "0.146.0",
            None,
            CodexBrokerFailure.VERSION_UNSUPPORTED,
        ),
        (
            "schema",
            False,
            "0.145.0",
            None,
            CodexBrokerFailure.PROTOCOL_UNSUPPORTED,
        ),
        (
            "owner",
            True,
            "0.145.0",
            os.geteuid() + 1,
            CodexBrokerFailure.RUNTIME_UNSAFE,
        ),
    )
    for (
        name,
        external_auth,
        daemon_version,
        expected_user_id,
        expected_failure,
    ) in cases:
        root = tmp_path / name
        root.mkdir()
        schema_root = root / "schema"
        native_home = short_socket_root / name
        native_home.mkdir()
        native_auth = native_home / "auth.json"
        native_auth.write_bytes(_NATIVE_AUTH)
        os.chmod(native_auth, 0o600)
        write_codex_schema(schema_root, external_auth=external_auth)
        write_fake_managed_codex(root, schema_root, native_home)
        environment = {
            "HOME": str(root),
            "PATH": os.pathsep.join((str(root), os.environ["PATH"])),
        }
        server = executable.discover_codex_executable(environment)

        with FakeCodexDaemon(
            native_home,
            app_server_version=daemon_version,
        ) as daemon:
            configure_codex_daemon_lifecycle(
                root,
                native_home,
                daemon.socket_path,
                app_server_version=daemon_version,
            )
            with pytest.raises(CodexBrokerError) as rejected:
                _prepare_shared_runtime(
                    server,
                    native_home,
                    environment,
                    expected_user_id,
                )

            assert rejected.value.code is expected_failure
            assert daemon.installed_account_ids == ()
            assert native_auth.read_bytes() == _NATIVE_AUTH

    root = tmp_path / "update"
    root.mkdir()
    schema_root = root / "schema"
    native_home = short_socket_root / "update"
    native_home.mkdir()
    write_codex_schema(schema_root, external_auth=True)
    write_fake_managed_codex(
        root,
        schema_root,
        native_home,
        version="0.146.0",
    )
    environment = {
        "HOME": str(root),
        "PATH": os.pathsep.join((str(root), os.environ["PATH"])),
    }
    server = executable.discover_codex_executable(environment)
    with FakeCodexDaemon(
        native_home,
        app_server_version="0.146.0",
    ) as daemon:
        lifecycle = configure_codex_daemon_lifecycle(
            root,
            native_home,
            daemon.socket_path,
            app_server_version="0.144.0",
            cli_version="0.146.0",
            already_running=True,
        )
        runtime = _prepare_shared_runtime(
            server,
            native_home,
            environment,
            None,
        )
        runtime.close()

    assert lifecycle.start_statuses == ("alreadyRunning",)
    assert lifecycle.restart_count == 1
    assert lifecycle.version_count == 1
    assert daemon.config_read_count == 1
    assert not (native_home / "auth.json").exists()

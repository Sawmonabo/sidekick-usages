"""Load-bearing tests for the versioned Codex app-server boundary."""

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from threading import Condition, Thread

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    TurnId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.protocol import FramedTransport
from sidekick_usages.platform.models import ProcessIdentity
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
from sidekick_usages.providers.codex.session.config import CodexSessionConfig
from sidekick_usages.providers.codex.session.models import (
    CODEX_SESSION_OPERATOR_PRECONDITION,
    CodexRelayAdmission,
    CodexRelayAdmissionState,
    CodexRelayAuthority,
    CodexSessionCapability,
    CodexSessionConfigurationReason,
)
from sidekick_usages.providers.codex.session.quiescence import (
    CodexParticipantProofChannel,
    CodexParticipantProofError,
    CodexParticipantProofSet,
)
from sidekick_usages.providers.codex.session.relay import CodexAdmissionRelay
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
    "v2/McpServerRefreshResponse.json",
    "v2/McpServerStatusUpdatedNotification.json",
)
_ROUTING_REQUEST_ID = 7
_RELAY_WAIT_SECONDS = 5.0
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
_PARTICIPANT_ID = ParticipantId("55555555-5555-4555-8555-555555555555")
_PARTICIPANT_PEER = ProcessIdentity(1234, 5678)


def _target_ready_proof() -> AuthorityReadyProof:
    return AuthorityReadyProof(
        provider_id=ProviderId.CODEX,
        account_id=_TARGET_AUTHORITY.account_id,
        generation=_TARGET_AUTHORITY.generation,
        epoch=_TARGET_AUTHORITY.epoch,
        safe_code=SelectionCode.SELECTION_SUCCEEDED,
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
        self._condition = Condition()
        self._authority = authority
        self._open = True
        self.begun: list[TurnId] = []
        self.ended: list[TurnId] = []
        self.ready_targets: list[CodexRelayAuthority] = []
        self.adoptions: list[tuple[TurnId, CodexRelayAuthority]] = []
        self.mcp_names: tuple[str, ...] = ()
        self.mcp_thread_ids: list[str] = []

    def request(self, method: str, params: JsonObject) -> JsonObject:
        """Return an exact MCP response for one loaded thread."""
        assert method == "mcpServerStatus/list"
        assert set(params) == {"threadId"}
        self.mcp_thread_ids.append(str(params["threadId"]))
        return {
            "data": [{"name": name} for name in self.mcp_names],
            "nextCursor": None,
        }

    def close_gate(self) -> None:
        with self._condition:
            self._open = False

    def open_gate(self, authority: CodexRelayAuthority) -> None:
        with self._condition:
            self._authority = authority
            self._open = True

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
            self.begun.append(turn_id)
            self._condition.notify_all()
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
            self.adoptions.append((turn_id, target))
            self._condition.notify_all()


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


def _serve_selection(
    channel: CodexParticipantProofChannel,
    relay: CodexAdmissionRelay,
) -> Thread:
    target = channel.serve_selection
    thread = Thread(target=target, args=(relay, _TARGET_AUTHORITY.epoch))
    thread.start()
    return thread


def _prove_nonempty_mcp_readback(
    relay: CodexAdmissionRelay,
    tui: FakeCodexTuiObserver,
    control: _RelayControl,
    daemon: FakeCodexDaemon,
    proofs: CodexParticipantProofSet,
    channel: CodexParticipantProofChannel,
    workers: tuple[Thread, ...],
) -> None:
    relay.open_epoch(_TARGET_AUTHORITY.epoch)
    threads = relay.loaded_threads_snapshot.thread_ids
    daemon.emit_mcp_statuses(tui, threads, ("starting", "ready"))
    readback_start = len(control.mcp_thread_ids)
    operation_id = OperationId("88888888-8888-4888-8888-888888888888")
    proof_thread = _serve_selection(channel, relay)
    proofs.bind_after_readback(operation_id, _TARGET_AUTHORITY)
    proof_thread.join(timeout=_RELAY_WAIT_SECONDS)
    assert not proof_thread.is_alive()
    assert proofs.matches_target(
        _PARTICIPANT_ID,
        1,
        _PARTICIPANT_PEER,
        operation_id,
        _target_ready_proof(),
    )
    assert control.mcp_thread_ids[readback_start:] == list(threads) * 2
    assert all(not worker.is_alive() for worker in workers)
    assert control.adoptions == [(control.begun[-1], _TARGET_AUTHORITY)]


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
    _send_turn(tui, 20, "thread-baseline")
    control.open_gate(_BASELINE_AUTHORITY)
    relay.reopen_baseline(_BASELINE_AUTHORITY)
    _receive_response(tui, 20)
    assert control.wait_count(control.ended, 3)
    assert daemon.relay_start_request_ids == (10, 11, 20)


def _prove_participant_quiescence(
    relay: CodexAdmissionRelay,
    tui: FakeCodexTuiObserver,
    control: _RelayControl,
    daemon: FakeCodexDaemon,
) -> None:
    failed_operation_id = OperationId("77777777-7777-4777-8777-777777777777")
    operation_id = OperationId("66666666-6666-4666-8666-666666666666")
    epoch = _TARGET_AUTHORITY.epoch
    supervisor_endpoint, participant_endpoint = socket.socketpair()
    proofs = CodexParticipantProofSet(FramedTransport)
    proofs.stage(
        _PARTICIPANT_ID,
        1,
        _PARTICIPANT_PEER,
        supervisor_endpoint,
    ).commit()
    channel = CodexParticipantProofChannel(
        participant_endpoint,
        FramedTransport,
    )
    control.close_gate()
    control.mcp_names = ("synthetic",)
    failed_threads = relay.loaded_threads_snapshot.thread_ids
    failed_worker = _serve_selection(channel, relay)
    proofs.prepare(failed_operation_id, epoch)
    daemon.emit_mcp_statuses(
        tui,
        failed_threads,
        ("starting", "ready"),
    )
    daemon.emit_mcp_status("thread-changed", "synthetic", "ready")
    assert tui.receive_optional(0.1) is None
    control.mcp_names = ("changed",)
    with pytest.raises(CodexParticipantProofError):
        proofs.confirm_baseline(failed_operation_id, epoch)
    proofs.abort(failed_operation_id, epoch)
    failed_worker.join(timeout=_RELAY_WAIT_SECONDS)
    control.mcp_names = ("synthetic",)
    _resume_thread(tui, 79, "thread-after-failed-proof")
    sealed_threads = relay.loaded_threads_snapshot.thread_ids
    proof_worker = _serve_selection(channel, relay)
    proofs.prepare(operation_id, epoch)
    daemon.emit_mcp_statuses(
        tui,
        sealed_threads,
        ("starting", "failed"),
    )
    proofs.confirm_baseline(operation_id, epoch)
    completion = Thread(
        target=proofs.complete,
        args=(operation_id, _TARGET_AUTHORITY),
    )
    completion.start()
    completion.join(timeout=0.1)
    assert completion.is_alive()
    daemon.emit_mcp_statuses(tui, sealed_threads, ("ready",))
    resumed = Thread(
        target=_resume_thread,
        args=(tui, 80, "thread-after-proof"),
    )
    resumed.start()
    resumed.join(timeout=0.1)
    completion.join(timeout=_RELAY_WAIT_SECONDS)
    proof_worker.join(timeout=_RELAY_WAIT_SECONDS)
    resumed.join(timeout=0.1)
    assert resumed.is_alive()
    relay.mark_ready(_TARGET_AUTHORITY, relay.loaded_threads_snapshot)
    _send_turn(tui, 70, "thread-target")
    control.open_gate(_TARGET_AUTHORITY)
    relay.open_epoch(_TARGET_AUTHORITY.epoch)
    relay.discard_quiescence()
    resumed.join(timeout=_RELAY_WAIT_SECONDS)
    _receive_response(tui, 70)
    assert control.wait_count(control.ended, 4)
    _prove_nonempty_mcp_readback(
        relay,
        tui,
        control,
        daemon,
        proofs,
        channel,
        (failed_worker, proof_worker, completion, resumed),
    )
    channel.close()
    proofs.close()


def _prove_versioned_relay_journey(short_socket_root: Path) -> None:
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
                _prove_participant_quiescence(
                    relay,
                    tui,
                    control,
                    daemon,
                )
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
    (native_home / "config.toml").write_bytes(
        CodexSessionConfig(native_home).prepare(None)
    )
    runtime = CodexSharedRuntime.create(
        executable,
        native_home,
        environment=environment,
        expected_user_id=expected_user_id,
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
    expected_settings = CodexSessionConfig(session_home).prepare(
        unrelated_settings
    )
    session_settings.write_bytes(expected_settings)
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
            assert session_settings.read_bytes() == expected_settings
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
        assert session_settings.read_bytes() == expected_settings
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

"""Load-bearing tests for the versioned Codex app-server boundary."""

import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

import sidekick_usages.providers.codex.app_server.capabilities
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.providers.codex.app_server.capabilities import (
    probe_codex_capabilities,
)
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
    verify_codex_executable,
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
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from sidekick_usages.providers.codex.session.models import (
    CODEX_SESSION_OPERATOR_PRECONDITION,
    CodexSessionCapability,
    CodexSessionConfigurationReason,
)
from sidekick_usages.serialization.json import JsonObject
from tests.fakes.codex.app_server.daemon import FakeCodexDaemon
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
_CAPABILITY_EXECUTABLE_VERIFICATIONS = 2
_ACCOUNT_ID = SidekickAccountId("33333333-3333-4333-8333-333333333333")
_PROVIDER_IDENTITY = "workspace-account-alpha"
_GENERATION = "2026-07-24T10:00:00.000000000Z"
_NATIVE_AUTH = managed_auth(_PROVIDER_IDENTITY, _GENERATION)
_SESSION_PROVIDER = "sidekick-chatgpt-http"
_SESSION_BASE_URL = "https://chatgpt.com/backend-api/codex"
_SYNTHETIC_MODEL_ATTEMPTS = 2
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


def _prove_synthetic_current_auth_http(
    capability: CodexSessionCapability,
) -> None:
    """Prove only the synthetic model-attempt selection boundary."""
    accounts = (
        ProviderIdentity("synthetic-account-a"),
        ProviderIdentity("synthetic-account-b"),
    )
    current_accounts = iter(accounts)
    model_attempt = SyntheticCodexModelAttempt(
        capability,
        lambda: next(current_accounts),
    )
    model_attempt.attempt()
    model_attempt.attempt()
    assert model_attempt.auth_resolutions == _SYNTHETIC_MODEL_ATTEMPTS
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
    )
    runtime.prepare(
        _ACCOUNT_ID,
        ProviderIdentity(_PROVIDER_IDENTITY),
        AuthorityGeneration(_GENERATION),
    )
    return runtime


def test_versioned_codex_app_server_boundary_is_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_root = tmp_path / "schema"
    write_codex_schema(schema_root, external_auth=True)
    executable_path = write_fake_codex(
        tmp_path,
        schema_root,
        version=_NEWER_CODEX_VERSION,
    )
    codex_home = tmp_path / "private-codex-home"
    codex_home.mkdir()
    environment = {
        "HOME": str(tmp_path),
        "OPENAI_API_KEY": RAW_PROVIDER_SECRET,
        "PATH": os.pathsep.join((str(tmp_path), os.environ["PATH"])),
    }

    verification_calls = 0

    def record_verification(executable: CodexExecutable) -> None:
        nonlocal verification_calls
        verification_calls += 1
        verify_codex_executable(executable)

    monkeypatch.setattr(
        sidekick_usages.providers.codex.app_server.capabilities,
        "verify_codex_executable",
        record_verification,
    )
    executable = discover_codex_executable(environment)
    capabilities = probe_codex_capabilities(executable, environment)
    with CodexAppServerSession.open(
        capabilities,
        codex_home,
        environment,
    ) as session:
        result = session.request(
            "account/read",
            {"refreshToken": False},
        )
        notification = session.receive()

        assert executable.provenance.path == executable_path.resolve()
        assert str(executable.version) == _NEWER_CODEX_VERSION
        assert verification_calls == _CAPABILITY_EXECUTABLE_VERIFICATIONS
        assert len(capabilities.schema_hash) == SCHEMA_HASH_HEX_LENGTH
        assert capabilities.session_schema_manifest == (
            _SESSION_SCHEMA_MANIFEST
        )
        assert result["requiresOpenaiAuth"] is True
        assert isinstance(notification, JsonRpcNotification)
        assert notification.method == "account/updated"
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
    assert Path(events[1]["argv"][4]).name == "schema"
    assert not Path(events[1]["argv"][4]).exists()
    assert events[2]["argv"] == ["app-server"]
    assert events[3]["method"] == "getAuthStatus"
    assert events[3]["params"] == {
        "includeToken": False,
        "refreshToken": False,
    }
    assert not Path(events[3]["codex_home"]).exists()
    assert all(event.get("openai_api_key") is None for event in events)
    assert events[-1]["codex_home"] == str(codex_home)


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
    executable = discover_codex_executable(environment)
    capabilities = probe_codex_capabilities(executable, environment)
    codex_home = tmp_path / "private-codex-home"
    codex_home.mkdir()
    (tmp_path / "mode").write_text("malformed", encoding="utf-8")

    with pytest.raises(CodexAppServerError) as malformed:
        CodexAppServerSession.open(
            capabilities,
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
    executable = discover_codex_executable(environment)

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
            executable,
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
            _prove_synthetic_current_auth_http(capability)
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
        executable = discover_codex_executable(environment)

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
                    executable,
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
    executable = discover_codex_executable(environment)
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
            executable,
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

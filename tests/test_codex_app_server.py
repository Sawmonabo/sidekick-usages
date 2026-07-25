"""Load-bearing tests for the versioned Codex app-server boundary."""

import json
import os
import sys
from pathlib import Path

import pytest

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
from tests.fakes.codex.auth import managed_auth
from tests.fakes.codex.daemon import FakeCodexDaemon
from tests.fakes.codex.executable import (
    RAW_PROVIDER_SECRET,
    configure_codex_daemon_lifecycle,
    write_fake_codex,
    write_fake_managed_codex,
)
from tests.fakes.codex.schema import write_codex_schema

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Managed Codex runtimes require Linux, WSL, or macOS.",
)

SCHEMA_HASH_HEX_LENGTH = 64
_ACCOUNT_ID = SidekickAccountId("33333333-3333-4333-8333-333333333333")
_PROVIDER_IDENTITY = "workspace-account-alpha"
_GENERATION = "2026-07-24T10:00:00.000000000Z"
_NATIVE_AUTH = managed_auth(_PROVIDER_IDENTITY, _GENERATION)


def _prepare_shared_runtime(
    executable: CodexExecutable,
    native_home: Path,
    environment: dict[str, str],
    expected_user_id: int | None,
) -> None:
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


def test_versioned_codex_app_server_boundary_is_complete(
    tmp_path: Path,
) -> None:
    schema_root = tmp_path / "schema"
    write_codex_schema(schema_root, external_auth=True)
    executable_path = write_fake_codex(tmp_path, schema_root)
    codex_home = tmp_path / "private-codex-home"
    codex_home.mkdir()
    environment = {
        "HOME": str(tmp_path),
        "OPENAI_API_KEY": RAW_PROVIDER_SECRET,
        "PATH": os.pathsep.join((str(tmp_path), os.environ["PATH"])),
    }

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

        assert executable.path == executable_path.resolve()
        assert str(executable.version) == "0.145.0"
        assert len(capabilities.schema_hash) == SCHEMA_HASH_HEX_LENGTH
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


def test_shared_codex_runtime_rejects_each_preflight_authority(
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

"""Load-bearing tests for the versioned managed Codex runtime."""

import json
import os
from pathlib import Path

import pytest

from sidekick_usages.providers.codex.app_server.capabilities import (
    probe_codex_capabilities,
)
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
)
from sidekick_usages.providers.codex.app_server.models import (
    JsonRpcNotification,
)
from sidekick_usages.providers.codex.app_server.session import (
    CodexAppServerSession,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from tests.fakes.codex import (
    RAW_PROVIDER_SECRET,
    write_codex_schema,
    write_fake_codex,
)

SCHEMA_HASH_HEX_LENGTH = 64
NEXT_REQUEST_AFTER_ACCOUNT_READ = 3


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
        assert session.next_request_id == NEXT_REQUEST_AFTER_ACCOUNT_READ
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
    write_codex_schema(schema_root, external_auth=False)
    write_fake_codex(tmp_path, schema_root)
    environment = {
        "HOME": str(tmp_path),
        "OPENAI_API_KEY": RAW_PROVIDER_SECRET,
        "PATH": os.pathsep.join((str(tmp_path), os.environ["PATH"])),
    }
    executable = discover_codex_executable(environment)

    with pytest.raises(CodexAppServerError) as unsupported:
        probe_codex_capabilities(executable, environment)

    assert unsupported.value.code is (
        CodexAppServerFailure.CAPABILITY_UNSUPPORTED
    )
    events = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert ["app-server"] not in [event["argv"] for event in events]

    write_codex_schema(schema_root, external_auth=True)
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

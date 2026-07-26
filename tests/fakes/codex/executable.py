"""Synthetic Codex executable and official daemon lifecycle."""

import json
import os
import sys
import textwrap
from collections.abc import Mapping
from pathlib import Path

from sidekick_usages.core.accounts.types import OperationId
from tests.fakes.codex.auth import NEXT_AUTH_FILE, managed_auth
from tests.fakes.codex.models import FakeCodexLogin, FakeWorkerRoute

RAW_PROVIDER_SECRET = "raw-provider-secret"
LOGIN_CONFIG_FILE = "login-config.json"
DAEMON_CONFIG_FILE = "daemon-config.json"
DAEMON_EVENTS_FILE = "daemon-events.jsonl"
HUNG_WORKER_MARKER_FILE = "hung-worker.started"


class FakeCodexDaemonLifecycle:
    """Safe observations of fake official lifecycle calls."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def start_statuses(self) -> tuple[str, ...]:
        """Return the fake daemon start results in call order."""
        return tuple(
            event["status"]
            for event in self._events()
            if event["operation"] == "start"
        )

    @property
    def version_count(self) -> int:
        """Return the number of official version inspections."""
        return sum(event["operation"] == "version" for event in self._events())

    def _events(self) -> tuple[dict[str, str], ...]:
        path = self._root / DAEMON_EVENTS_FILE
        if not path.exists():
            return ()
        events = tuple(
            json.loads(line) for line in path.read_text().splitlines()
        )
        if not all(
            isinstance(event, dict)
            and isinstance(event.get("operation"), str)
            and isinstance(event.get("status"), str)
            for event in events
        ):
            raise AssertionError("Fake Codex lifecycle events are malformed.")
        return events


def configure_codex_logins(
    root: Path,
    logins: Mapping[Path, FakeCodexLogin],
) -> None:
    """Configure synthetic official login results by exact final home."""
    payload = {
        str(home): {
            "login_auth": managed_auth(
                login.provider_identity,
                login.login_generation,
            ).decode(),
            "outcome": login.outcome,
            "refresh_auth": managed_auth(
                login.provider_identity,
                login.refresh_generation,
            ).decode(),
        }
        for home, login in logins.items()
    }
    (root / LOGIN_CONFIG_FILE).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    (root / "mode").write_text("normal", encoding="utf-8")


def write_fake_managed_codex(
    root: Path,
    schema_root: Path,
    native_home: Path,
) -> None:
    """Write a fake executable and official-shaped managed installation."""
    executable = write_fake_codex(root, schema_root)
    managed = native_home.joinpath(
        "packages",
        "standalone",
        "current",
        "codex",
    )
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.symlink_to(executable)


def write_worker_router(
    root: Path,
    hung_operation_id: OperationId,
    worker_executable: Path,
) -> FakeWorkerRoute:
    """Route one operation to a stubborn process and all others to Sidekick."""
    executable = root / "sidekick-usages-worker"
    started = root / HUNG_WORKER_MARKER_FILE
    executable.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import os
            import signal
            import sys
            import time
            from pathlib import Path

            HUNG_OPERATION_ID = {json.dumps(str(hung_operation_id))}
            REAL_WORKER = {json.dumps(str(worker_executable))}
            STARTED = Path({json.dumps(str(started))})

            if sys.argv[1:] == [HUNG_OPERATION_ID]:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                STARTED.write_text(str(os.getpid()), encoding="utf-8")
                while True:
                    time.sleep(1)
            os.execv(REAL_WORKER, [REAL_WORKER, *sys.argv[1:]])
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return FakeWorkerRoute(executable, started)


def configure_codex_daemon_lifecycle(
    root: Path,
    native_home: Path,
    socket_path: Path,
    *,
    app_server_version: str = "0.145.0",
) -> FakeCodexDaemonLifecycle:
    """Configure exact start and version responses for the fake executable."""
    managed = native_home.joinpath(
        "packages",
        "standalone",
        "current",
        "codex",
    )
    payload = {
        "app_server_version": app_server_version,
        "managed_codex_path": str(managed),
        "pid": os.getpid(),
        "socket_path": str(socket_path),
    }
    (root / DAEMON_CONFIG_FILE).write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    return FakeCodexDaemonLifecycle(root)


def write_fake_codex(tmp_path: Path, schema_root: Path) -> Path:
    """Write one fake supporting version, schema, stdio, and lifecycle."""
    executable = tmp_path / "codex"
    mode_path = tmp_path / "mode"
    events_path = tmp_path / "events.jsonl"
    pid_path = tmp_path / "app-server.pid"
    executable.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            import shutil
            import signal
            import sys
            import time
            from pathlib import Path

            SCHEMA_ROOT = Path({json.dumps(str(schema_root))})
            MODE_PATH = Path({json.dumps(str(mode_path))})
            EVENTS_PATH = Path({json.dumps(str(events_path))})
            PID_PATH = Path({json.dumps(str(pid_path))})
            RAW_SECRET = {json.dumps(RAW_PROVIDER_SECRET)}
            NEXT_AUTH_FILE = {json.dumps(NEXT_AUTH_FILE)}
            LOGIN_CONFIG_FILE = Path(
                {json.dumps(str(tmp_path / LOGIN_CONFIG_FILE))}
            )
            DAEMON_CONFIG_FILE = Path(
                {json.dumps(str(tmp_path / DAEMON_CONFIG_FILE))}
            )
            DAEMON_EVENTS_FILE = Path(
                {json.dumps(str(tmp_path / DAEMON_EVENTS_FILE))}
            )
            EMITTED_AT_MILLISECONDS = 1_750_000_000_000

            def login_config():
                if not LOGIN_CONFIG_FILE.exists():
                    return None
                configured = json.loads(
                    LOGIN_CONFIG_FILE.read_text(encoding="utf-8")
                )
                return configured.get(os.environ["CODEX_HOME"])

            def write_auth(home, payload):
                auth_path = home / "auth.json"
                auth_path.write_text(payload, encoding="utf-8")
                os.chmod(auth_path, 0o600)

            def daemon_lifecycle(operation):
                configured = json.loads(
                    DAEMON_CONFIG_FILE.read_text(encoding="utf-8")
                )
                prior = []
                if DAEMON_EVENTS_FILE.exists():
                    prior = [
                        json.loads(line)
                        for line in DAEMON_EVENTS_FILE.read_text().splitlines()
                    ]
                if operation == "start":
                    status = (
                        "alreadyRunning"
                        if any(
                            event["operation"] == "start"
                            for event in prior
                        )
                        else "started"
                    )
                else:
                    status = "running"
                event = {{"operation": operation, "status": status}}
                with DAEMON_EVENTS_FILE.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(event) + "\\n")
                version = configured["app_server_version"]
                response = {{
                    "appServerVersion": version,
                    "backend": "pid",
                    "cliVersion": version,
                    "managedCodexPath": configured["managed_codex_path"],
                    "managedCodexVersion": version,
                    "socketPath": configured["socket_path"],
                    "status": status,
                }}
                if status == "started":
                    response["pid"] = configured["pid"]
                print(json.dumps(response))

            event = {{
                "argv": sys.argv[1:],
                "codex_home": os.environ.get("CODEX_HOME"),
                "cwd": os.getcwd(),
                "openai_api_key": os.environ.get("OPENAI_API_KEY"),
            }}
            with EVENTS_PATH.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event) + "\\n")

            if sys.argv[1:] == ["--version"]:
                print("codex-cli 0.145.0")
                raise SystemExit
            if sys.argv[1:4] == [
                "app-server",
                "generate-json-schema",
                "--experimental",
            ]:
                output = Path(sys.argv[5])
                shutil.copytree(SCHEMA_ROOT, output, dirs_exist_ok=True)
                raise SystemExit
            if sys.argv[1:] == ["app-server", "daemon", "start"]:
                daemon_lifecycle("start")
                raise SystemExit
            if sys.argv[1:] == ["app-server", "daemon", "version"]:
                daemon_lifecycle("version")
                raise SystemExit
            if sys.argv[1:] != ["app-server"]:
                raise SystemExit(2)

            PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
            mode = MODE_PATH.read_text(encoding="utf-8").strip()
            for line in sys.stdin:
                request = json.loads(line)
                if request["method"] == "initialized":
                    continue
                request_id = request["id"]
                if mode == "malformed":
                    print(
                        '{{"id":1,"result":{{"secret":"'
                        + RAW_SECRET
                        + '"}},"result":{{}}}}',
                        flush=True,
                    )
                    continue
                if request["method"] == "initialize":
                    originator = request["params"]["clientInfo"]["name"]
                    result = {{
                        "codexHome": os.environ["CODEX_HOME"],
                        "platformFamily": "unix",
                        "platformOs": "linux",
                        "userAgent": (
                            f"{{originator}}/0.145.0 (fake 1; x86_64)"
                        ),
                    }}
                    print(
                        json.dumps({{"id": request_id, "result": result}}),
                        flush=True,
                    )
                elif request["method"] == "account/login/start":
                    with EVENTS_PATH.open("a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps(
                                {{
                                    "codex_home": os.environ["CODEX_HOME"],
                                    "cwd": os.getcwd(),
                                    "method": request["method"],
                                    "params": request["params"],
                                }}
                            )
                            + "\\n"
                        )
                    configured = login_config()
                    if configured is None:
                        print(
                            json.dumps(
                                {{
                                    "id": request_id,
                                    "error": {{
                                        "code": -32000,
                                        "message": RAW_SECRET,
                                    }},
                                }}
                            ),
                            flush=True,
                        )
                        continue
                    print(
                            json.dumps(
                                {{
                                    "emittedAtMs": EMITTED_AT_MILLISECONDS,
                                    "method": "configWarning",
                                    "params": {{
                                        "message": "synthetic warning"
                                    }},
                                }}
                        ),
                        flush=True,
                    )
                    home = Path(os.environ["CODEX_HOME"])
                    login_id = "login-" + home.name
                    login_type = request["params"]["type"]
                    if login_type == "chatgptDeviceCode":
                        result = {{
                            "loginId": login_id,
                            "type": login_type,
                            "userCode": "SAFE-CODE",
                            "verificationUrl": (
                                "https://auth.openai.com/codex/device"
                            ),
                        }}
                    else:
                        result = {{
                            "authUrl": (
                                "https://auth.openai.com/oauth/authorize"
                                "?state=synthetic"
                            ),
                            "loginId": login_id,
                            "type": login_type,
                        }}
                    print(
                        json.dumps({{"id": request_id, "result": result}}),
                        flush=True,
                    )
                    succeeded = configured["outcome"] == "success"
                    if succeeded:
                        write_auth(home, configured["login_auth"])
                    print(
                        json.dumps(
                            {{
                                "emittedAtMs": EMITTED_AT_MILLISECONDS,
                                "method": "account/login/completed",
                                "params": {{
                                    "error": (
                                        None if succeeded else RAW_SECRET
                                    ),
                                    "loginId": login_id,
                                    "success": succeeded,
                                }},
                            }}
                        ),
                        flush=True,
                    )
                    if succeeded:
                        print(
                            json.dumps(
                                {{
                                    "emittedAtMs": EMITTED_AT_MILLISECONDS,
                                    "method": "account/updated",
                                    "params": {{
                                        "authMode": "chatgpt",
                                        "planType": "pro",
                                    }},
                                }}
                            ),
                            flush=True,
                        )
                elif request["method"] == "account/read":
                    with EVENTS_PATH.open("a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps(
                                {{
                                    "codex_home": os.environ["CODEX_HOME"],
                                    "cwd": os.getcwd(),
                                    "method": request["method"],
                                    "params": request["params"],
                                }}
                            )
                            + "\\n"
                        )
                    if request["params"]["refreshToken"] is True:
                        home = Path(os.environ["CODEX_HOME"])
                        next_auth = home / NEXT_AUTH_FILE
                        if next_auth.exists():
                            os.replace(next_auth, home / "auth.json")
                        else:
                            configured = login_config()
                            if configured is not None:
                                write_auth(home, configured["refresh_auth"])
                    notification = {{
                        "emittedAtMs": EMITTED_AT_MILLISECONDS,
                        "method": "account/updated",
                        "params": {{
                            "authMode": "chatgpt",
                            "planType": "pro",
                        }},
                    }}
                    result = {{
                        "account": {{
                            "email": None,
                            "planType": "pro",
                            "type": "chatgpt",
                        }},
                        "requiresOpenaiAuth": True,
                    }}
                    print(json.dumps(notification), flush=True)
                    print(
                        json.dumps({{"id": request_id, "result": result}}),
                        flush=True,
                    )
                elif request["method"] == "getAuthStatus":
                    with EVENTS_PATH.open("a", encoding="utf-8") as stream:
                        stream.write(
                            json.dumps(
                                {{
                                    "codex_home": os.environ["CODEX_HOME"],
                                    "cwd": os.getcwd(),
                                    "method": request["method"],
                                    "params": request["params"],
                                }}
                            )
                            + "\\n"
                        )
                    home = Path(os.environ["CODEX_HOME"])
                    auth_path = home / "auth.json"
                    include_token = request["params"]["includeToken"]
                    token = None
                    if include_token is True and auth_path.exists():
                        auth = json.loads(
                            auth_path.read_text(encoding="utf-8")
                        )
                        token = auth["tokens"]["access_token"]
                    result = {{
                        "authMethod": (
                            None if token is None else "chatgpt"
                        ),
                        "authToken": token,
                        "requiresOpenaiAuth": True,
                    }}
                    print(
                        json.dumps({{"id": request_id, "result": result}}),
                        flush=True,
                    )
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            while mode == "stubborn":
                time.sleep(1)
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    mode_path.write_text("stubborn", encoding="utf-8")
    return executable

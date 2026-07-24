"""Small executable and generated-schema fake for Codex boundary tests."""

import json
import sys
import textwrap
from pathlib import Path

from sidekick_usages.serialization.json import JsonObject, JsonValue

RAW_PROVIDER_SECRET = "raw-provider-secret"


def write_codex_schema(root: Path, *, external_auth: bool) -> None:
    """Write the minimal release-shaped schema required by the probe."""
    login_variants: list[JsonValue] = [
        _variant("chatgpt"),
        _variant("chatgptDeviceCode"),
    ]
    login_response_variants: list[JsonValue] = [
        _variant("chatgpt", "authUrl", "loginId"),
        _variant(
            "chatgptDeviceCode",
            "loginId",
            "userCode",
            "verificationUrl",
        ),
    ]
    if external_auth:
        login_variants.append(
            _variant(
                "chatgptAuthTokens",
                "accessToken",
                "chatgptAccountId",
            )
        )
        login_response_variants.append(_variant("chatgptAuthTokens"))
    account_variants: list[JsonValue] = [
        _variant("chatgpt", "email", "planType")
    ]
    schemas_by_path: dict[str, JsonObject] = {
        "v1/InitializeParams.json": _object_schema(
            {"clientInfo": {"type": "object"}},
            required=("clientInfo",),
            definitions={
                "InitializeCapabilities": _object_schema(
                    {"experimentalApi": {"type": "boolean"}}
                )
            },
        ),
        "v1/InitializeResponse.json": _object_schema(
            {
                "codexHome": {"type": "string"},
                "platformFamily": {"type": "string"},
                "platformOs": {"type": "string"},
                "userAgent": {"type": "string"},
            },
            required=(
                "codexHome",
                "platformFamily",
                "platformOs",
                "userAgent",
            ),
        ),
        "v2/GetAccountParams.json": _object_schema(
            {"refreshToken": {"type": "boolean"}}
        ),
        "v2/GetAccountResponse.json": _object_schema(
            {
                "account": {"type": _json_values("object", "null")},
                "requiresOpenaiAuth": {"type": "boolean"},
            },
            required=("requiresOpenaiAuth",),
            definitions={"Account": {"oneOf": account_variants}},
        ),
        "v2/LoginAccountParams.json": {"oneOf": login_variants},
        "v2/LoginAccountResponse.json": {"oneOf": login_response_variants},
        "v2/AccountUpdatedNotification.json": _object_schema(
            {
                "authMode": {
                    "type": _json_values("string", "null"),
                    "enum": _json_values(
                        "chatgpt",
                        "chatgptAuthTokens",
                        None,
                    ),
                },
                "planType": {"type": _json_values("string", "null")},
            }
        ),
        "ChatgptAuthTokensRefreshParams.json": _object_schema(
            {
                "previousAccountId": {"type": _json_values("string", "null")},
                "reason": {
                    "type": "string",
                    "enum": _json_values("unauthorized"),
                },
            },
            required=("reason",),
        ),
        "ChatgptAuthTokensRefreshResponse.json": _object_schema(
            {
                "accessToken": {"type": "string"},
                "chatgptAccountId": {"type": "string"},
                "chatgptPlanType": {"type": _json_values("string", "null")},
            },
            required=("accessToken", "chatgptAccountId"),
        ),
        "ClientRequest.json": _method_schema(
            "initialize",
            "account/login/start",
            "account/read",
        ),
        "ClientNotification.json": _method_schema("initialized"),
        "ServerRequest.json": _method_schema(
            "account/chatgptAuthTokens/refresh"
        ),
        "ServerNotification.json": _method_schema("account/updated"),
    }
    for relative, payload in schemas_by_path.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")


def write_fake_codex(tmp_path: Path, schema_root: Path) -> Path:
    """Write one fake that supports version, schema, and stdio modes."""
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

            event = {{
                "argv": sys.argv[1:],
                "codex_home": os.environ.get("CODEX_HOME"),
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
                    result = {{
                        "codexHome": os.environ["CODEX_HOME"],
                        "platformFamily": "unix",
                        "platformOs": "linux",
                        "userAgent": "fake-codex",
                    }}
                    print(
                        json.dumps({{"id": request_id, "result": result}}),
                        flush=True,
                    )
                elif request["method"] == "account/read":
                    notification = {{
                        "method": "account/updated",
                        "params": {{
                            "authMode": "chatgpt",
                            "planType": "pro",
                        }},
                    }}
                    result = {{
                        "account": {{
                            "email": "person@example.test",
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


def _object_schema(
    properties: JsonObject,
    *,
    required: tuple[str, ...] = (),
    definitions: JsonObject | None = None,
) -> JsonObject:
    schema: JsonObject = {"type": "object", "properties": properties}
    if required:
        schema["required"] = _json_values(*required)
    if definitions is not None:
        schema["definitions"] = definitions
    return schema


def _type_property(value: str) -> JsonObject:
    return {"type": "string", "enum": _json_values(value)}


def _variant(value: str, *required: str) -> JsonObject:
    properties: JsonObject = {"type": _type_property(value)}
    for name in required:
        properties[name] = {"type": "string"}
    return _object_schema(
        properties,
        required=("type", *required),
    )


def _method_schema(*methods: str) -> JsonObject:
    variants: list[JsonValue] = [
        _object_schema(
            {"method": _type_property(method)},
            required=("method",),
        )
        for method in methods
    ]
    return {"oneOf": variants}


def _json_values(*values: JsonValue) -> list[JsonValue]:
    return list(values)

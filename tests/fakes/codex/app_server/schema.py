"""Release-shaped generated Codex schemas for capability tests."""

import json
from pathlib import Path

from sidekick_usages.serialization.json import JsonObject, JsonValue


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
        external_variant = _variant(
            "chatgptAuthTokens",
            "accessToken",
            "chatgptAccountId",
        )
        properties = external_variant["properties"]
        assert isinstance(properties, dict)
        properties["chatgptPlanType"] = {
            "type": _json_values("string", "null")
        }
        login_variants.append(external_variant)
        login_response_variants.append(_variant("chatgptAuthTokens"))
    account_variants: list[JsonValue] = [
        _variant("chatgpt", "email", "planType")
    ]
    server_notifications = _method_schema(
        "account/login/completed",
        "account/updated",
        "turn/started",
        "turn/completed",
        "thread/realtime/started",
        "thread/realtime/closed",
        "mcpServer/startupStatus/updated",
    )
    server_notifications["properties"] = {"emittedAtMs": {"type": "integer"}}
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
        "v2/AccountLoginCompletedNotification.json": _object_schema(
            {
                "error": {"type": _json_values("string", "null")},
                "loginId": {"type": _json_values("string", "null")},
                "success": {"type": "boolean"},
            },
            required=("success",),
        ),
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
        "v2/ConfigReadParams.json": _object_schema(
            {
                "cwd": {"type": _json_values("string", "null")},
                "includeLayers": {"type": "boolean"},
            }
        ),
        "v2/ConfigReadResponse.json": _object_schema(
            {
                "config": {"type": "object"},
                "layers": {"type": _json_values("array", "null")},
                "origins": {"type": "object"},
            },
            required=("config", "origins"),
            definitions={
                "ConfigLayer": _object_schema(
                    {
                        "config": {"type": "object"},
                        "name": {"type": "object"},
                        "version": {"type": "string"},
                    },
                    required=("config", "name", "version"),
                ),
                "ConfigLayerMetadata": _object_schema(
                    {
                        "name": {"type": "object"},
                        "version": {"type": "string"},
                    },
                    required=("name", "version"),
                ),
                "ConfigLayerSource": {
                    "oneOf": [
                        _variant("user", "file"),
                        _variant("project", "dotCodexFolder"),
                        _variant("sessionFlags"),
                    ]
                },
            },
        ),
        "v2/ModelProviderCapabilitiesReadParams.json": _object_schema({}),
        "v2/ModelProviderCapabilitiesReadResponse.json": _object_schema(
            {
                "imageGeneration": {"type": "boolean"},
                "namespaceTools": {"type": "boolean"},
                "webSearch": {"type": "boolean"},
            },
            required=("imageGeneration", "namespaceTools", "webSearch"),
        ),
        "v2/TurnStartParams.json": _object_schema(
            {
                "input": {"type": "array"},
                "threadId": {"type": "string"},
            },
            required=("input", "threadId"),
        ),
        "v2/TurnStartResponse.json": _object_schema(
            {"turn": {"type": "object"}},
            required=("turn",),
        ),
        "v2/TurnStartedNotification.json": _object_schema(
            {
                "threadId": {"type": "string"},
                "turn": {"type": "object"},
            },
            required=("threadId", "turn"),
        ),
        "v2/TurnCompletedNotification.json": _object_schema(
            {
                "threadId": {"type": "string"},
                "turn": {"type": "object"},
            },
            required=("threadId", "turn"),
        ),
        "v2/ThreadRealtimeStartParams.json": _object_schema(
            {
                "outputModality": {"type": "string"},
                "threadId": {"type": "string"},
            },
            required=("outputModality", "threadId"),
        ),
        "v2/ThreadRealtimeStartResponse.json": _object_schema({}),
        "v2/ThreadRealtimeStartedNotification.json": _object_schema(
            {
                "threadId": {"type": "string"},
                "version": {"type": "string"},
            },
            required=("threadId", "version"),
        ),
        "v2/ThreadRealtimeClosedNotification.json": _object_schema(
            {"threadId": {"type": "string"}},
            required=("threadId",),
        ),
        "v2/ListMcpServerStatusParams.json": _object_schema(
            {"threadId": {"type": _json_values("string", "null")}}
        ),
        "v2/ListMcpServerStatusResponse.json": _object_schema(
            {"data": {"type": "array"}},
            required=("data",),
        ),
        "v2/McpServerRefreshResponse.json": _object_schema({}),
        "v2/McpServerStatusUpdatedNotification.json": _object_schema(
            {
                "name": {"type": "string"},
                "status": {"type": "string"},
                "threadId": {"type": _json_values("string", "null")},
            },
            required=("name", "status"),
        ),
        "ClientRequest.json": _method_schema(
            "initialize",
            "account/login/cancel",
            "account/login/start",
            "account/logout",
            "account/read",
            "config/read",
            "config/mcpServer/reload",
            "modelProvider/capabilities/read",
            "turn/start",
            "thread/realtime/start",
            "mcpServerStatus/list",
        ),
        "ClientNotification.json": _method_schema("initialized"),
        "ServerRequest.json": _method_schema(
            "account/chatgptAuthTokens/refresh"
        ),
        "ServerNotification.json": server_notifications,
    }
    for relative, payload in schemas_by_path.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload), encoding="utf-8")


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

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
    )
    server_notifications["properties"] = {
        "emittedAtMs": {"type": "integer"}
    }
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
                "previousAccountId": {
                    "type": _json_values("string", "null")
                },
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
                "chatgptPlanType": {
                    "type": _json_values("string", "null")
                },
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

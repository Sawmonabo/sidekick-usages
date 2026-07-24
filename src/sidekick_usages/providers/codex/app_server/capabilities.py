"""Release-matched schema gate for managed Codex auth."""

import hashlib
import stat
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import NoReturn

from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.executable import (
    verify_codex_executable,
)
from sidekick_usages.providers.codex.app_server.models import (
    CodexAppServerCapabilities,
    CodexExecutable,
)
from sidekick_usages.providers.codex.app_server.process import (
    minimal_codex_environment,
    run_quiet_codex_command,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.serialization.json import (
    JsonObject,
    JsonValue,
    decode_json_object,
)

SCHEMA_FILES = (
    "v1/InitializeParams.json",
    "v1/InitializeResponse.json",
    "v2/GetAccountParams.json",
    "v2/GetAccountResponse.json",
    "v2/LoginAccountParams.json",
    "v2/LoginAccountResponse.json",
    "v2/AccountUpdatedNotification.json",
    "ChatgptAuthTokensRefreshParams.json",
    "ChatgptAuthTokensRefreshResponse.json",
    "ClientRequest.json",
    "ClientNotification.json",
    "ServerRequest.json",
    "ServerNotification.json",
)
_MAX_SCHEMA_FILE_BYTES = 512 * 1024
_MAX_SCHEMA_DEPTH = 32
_SCHEMA_COMMAND_TIMEOUT_SECONDS = 20.0


def probe_codex_capabilities(
    executable: CodexExecutable,
    environment: Mapping[str, str] | None = None,
) -> CodexAppServerCapabilities:
    """Generate and prove the exact required app-server schema surface."""
    verify_codex_executable(executable)
    with tempfile.TemporaryDirectory(
        prefix="sidekick-codex-schema-"
    ) as raw_root:
        temporary_root = Path(raw_root)
        codex_home = temporary_root / "codex-home"
        schema_directory = temporary_root / "schema"
        codex_home.mkdir(mode=0o700)
        schema_directory.mkdir(mode=0o700)
        run_quiet_codex_command(
            (
                str(executable.path),
                "app-server",
                "generate-json-schema",
                "--experimental",
                "--out",
                str(schema_directory),
            ),
            minimal_codex_environment(
                environment,
                codex_home=codex_home,
            ),
            timeout_seconds=_SCHEMA_COMMAND_TIMEOUT_SECONDS,
        )
        raw_schemas, schemas = _read_required_schemas(schema_directory)
        _validate_required_capabilities(schemas)
        schema_hash = _hash_schemas(raw_schemas)
    return CodexAppServerCapabilities(
        executable=executable,
        schema_hash=schema_hash,
    )


def _read_required_schemas(
    schema_directory: Path,
) -> tuple[dict[str, bytes], dict[str, JsonObject]]:
    raw_schemas: dict[str, bytes] = {}
    schemas: dict[str, JsonObject] = {}
    for relative in SCHEMA_FILES:
        path = schema_directory / relative
        try:
            file_status = path.lstat()
            if not stat.S_ISREG(file_status.st_mode):
                _unsupported()
            with path.open("rb") as stream:
                payload = stream.read(_MAX_SCHEMA_FILE_BYTES + 1)
        except OSError:
            raise CodexAppServerError(
                CodexAppServerFailure.CAPABILITY_UNSUPPORTED
            ) from None
        if len(payload) > _MAX_SCHEMA_FILE_BYTES:
            raise CodexAppServerError(
                CodexAppServerFailure.CAPABILITY_UNSUPPORTED
            )
        try:
            schema = decode_json_object(payload)
        except InvalidPayloadError:
            raise CodexAppServerError(
                CodexAppServerFailure.CAPABILITY_UNSUPPORTED
            ) from None
        raw_schemas[relative] = payload
        schemas[relative] = schema
    return raw_schemas, schemas


def _validate_required_capabilities(
    schemas: dict[str, JsonObject],
) -> None:
    initialize = schemas["v1/InitializeParams.json"]
    _require_names(initialize, "required", ("clientInfo",))
    _require_property_anywhere(initialize, "experimentalApi", "boolean")

    initialize_response = schemas["v1/InitializeResponse.json"]
    _require_names(
        initialize_response,
        "required",
        ("codexHome", "platformFamily", "platformOs", "userAgent"),
    )
    for name in ("codexHome", "platformFamily", "platformOs", "userAgent"):
        _require_property(initialize_response, name, "string")

    account_params = schemas["v2/GetAccountParams.json"]
    _require_property(account_params, "refreshToken", "boolean")
    account_response = schemas["v2/GetAccountResponse.json"]
    _require_names(
        account_response,
        "required",
        ("requiresOpenaiAuth",),
    )
    _require_property(account_response, "requiresOpenaiAuth", "boolean")
    _require_variant(
        account_response,
        "chatgpt",
        ("email", "planType", "type"),
    )

    login_params = schemas["v2/LoginAccountParams.json"]
    _require_variant(login_params, "chatgpt", ("type",))
    _require_variant(login_params, "chatgptDeviceCode", ("type",))
    _require_variant(
        login_params,
        "chatgptAuthTokens",
        ("accessToken", "chatgptAccountId", "type"),
    )
    login_response = schemas["v2/LoginAccountResponse.json"]
    _require_variant(
        login_response,
        "chatgpt",
        ("authUrl", "loginId", "type"),
    )
    _require_variant(
        login_response,
        "chatgptDeviceCode",
        ("loginId", "type", "userCode", "verificationUrl"),
    )
    _require_variant(login_response, "chatgptAuthTokens", ("type",))

    account_updated = schemas["v2/AccountUpdatedNotification.json"]
    _require_property_enum(
        account_updated,
        "authMode",
        "chatgptAuthTokens",
    )
    _require_property(account_updated, "planType", "string")

    refresh_params = schemas["ChatgptAuthTokensRefreshParams.json"]
    _require_names(refresh_params, "required", ("reason",))
    _require_property_enum(refresh_params, "reason", "unauthorized")
    _require_property(refresh_params, "previousAccountId", "string")
    refresh_response = schemas["ChatgptAuthTokensRefreshResponse.json"]
    _require_names(
        refresh_response,
        "required",
        ("accessToken", "chatgptAccountId"),
    )
    _require_property(refresh_response, "accessToken", "string")
    _require_property(refresh_response, "chatgptAccountId", "string")
    _require_property(refresh_response, "chatgptPlanType", "string")

    for method in ("initialize", "account/login/start", "account/read"):
        _require_method(schemas["ClientRequest.json"], method)
    _require_method(schemas["ClientNotification.json"], "initialized")
    _require_method(
        schemas["ServerRequest.json"],
        "account/chatgptAuthTokens/refresh",
    )
    _require_method(
        schemas["ServerNotification.json"],
        "account/updated",
    )


def _hash_schemas(raw_schemas: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative in SCHEMA_FILES:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw_schemas[relative])
        digest.update(b"\0")
    return digest.hexdigest()


def _require_names(
    schema: JsonObject,
    member: str,
    expected: tuple[str, ...],
) -> None:
    value = schema.get(member)
    if not isinstance(value, list) or not set(expected) <= {
        item for item in value if isinstance(item, str)
    }:
        _unsupported()


def _require_property(
    schema: JsonObject,
    name: str,
    expected_type: str,
) -> None:
    property_schema = _top_level_property(schema, name)
    if not _schema_allows_type(
        schema,
        property_schema,
        expected_type,
        depth=0,
    ):
        _unsupported()


def _require_property_anywhere(
    schema: JsonObject,
    name: str,
    expected_type: str,
) -> None:
    for candidate in _objects(schema, depth=0):
        properties = candidate.get("properties")
        if not isinstance(properties, dict):
            continue
        property_schema = properties.get(name)
        if isinstance(property_schema, dict) and _schema_allows_type(
            schema,
            property_schema,
            expected_type,
            depth=0,
        ):
            return
    _unsupported()


def _require_property_enum(
    schema: JsonObject,
    name: str,
    expected_value: str,
) -> None:
    property_schema = _top_level_property(schema, name)
    if not _schema_allows_enum(
        schema,
        property_schema,
        expected_value,
        depth=0,
    ):
        _unsupported()


def _require_variant(
    schema: JsonObject,
    type_value: str,
    required_names: tuple[str, ...],
) -> None:
    for candidate in _objects(schema, depth=0):
        properties = candidate.get("properties")
        required = candidate.get("required")
        if not isinstance(properties, dict) or not isinstance(required, list):
            continue
        type_schema = properties.get("type")
        if (
            isinstance(type_schema, dict)
            and _schema_allows_enum(
                schema,
                type_schema,
                type_value,
                depth=0,
            )
            and set(required_names)
            <= {item for item in required if isinstance(item, str)}
        ):
            return
    _unsupported()


def _require_method(schema: JsonObject, method: str) -> None:
    for candidate in _objects(schema, depth=0):
        properties = candidate.get("properties")
        if not isinstance(properties, dict):
            continue
        method_schema = properties.get("method")
        if isinstance(method_schema, dict) and _schema_allows_enum(
            schema,
            method_schema,
            method,
            depth=0,
        ):
            return
    _unsupported()


def _top_level_property(schema: JsonObject, name: str) -> JsonObject:
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        _unsupported()
    property_schema = properties.get(name)
    if not isinstance(property_schema, dict):
        _unsupported()
    return property_schema


def _schema_allows_type(
    root: JsonObject,
    schema: JsonObject,
    expected_type: str,
    *,
    depth: int,
) -> bool:
    if depth > _MAX_SCHEMA_DEPTH:
        return False
    declared = schema.get("type")
    if declared == expected_type:
        return True
    if isinstance(declared, list) and expected_type in declared:
        return True
    referenced = _referenced_schema(root, schema)
    if referenced is not None and _schema_allows_type(
        root,
        referenced,
        expected_type,
        depth=depth + 1,
    ):
        return True
    return any(
        _schema_allows_type(
            root,
            candidate,
            expected_type,
            depth=depth + 1,
        )
        for candidate in _composed_schemas(schema)
    )


def _schema_allows_enum(
    root: JsonObject,
    schema: JsonObject,
    expected_value: str,
    *,
    depth: int,
) -> bool:
    if depth > _MAX_SCHEMA_DEPTH:
        return False
    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and expected_value in enum_values:
        return True
    referenced = _referenced_schema(root, schema)
    if referenced is not None and _schema_allows_enum(
        root,
        referenced,
        expected_value,
        depth=depth + 1,
    ):
        return True
    return any(
        _schema_allows_enum(
            root,
            candidate,
            expected_value,
            depth=depth + 1,
        )
        for candidate in _composed_schemas(schema)
    )


def _referenced_schema(
    root: JsonObject,
    schema: JsonObject,
) -> JsonObject | None:
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith(
        "#/definitions/"
    ):
        return None
    definitions = root.get("definitions")
    if not isinstance(definitions, dict):
        return None
    target = definitions.get(reference.removeprefix("#/definitions/"))
    return target if isinstance(target, dict) else None


def _composed_schemas(schema: JsonObject) -> Iterator[JsonObject]:
    for keyword in ("allOf", "anyOf", "oneOf"):
        candidates = schema.get(keyword)
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if isinstance(candidate, dict):
                yield candidate


def _objects(value: JsonValue, *, depth: int) -> Iterator[JsonObject]:
    if depth > _MAX_SCHEMA_DEPTH:
        return
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child, depth=depth + 1)


def _unsupported() -> NoReturn:
    raise CodexAppServerError(CodexAppServerFailure.CAPABILITY_UNSUPPORTED)

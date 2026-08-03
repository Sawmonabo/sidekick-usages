"""Release-matched schema gate for managed Codex auth."""

import hashlib
import stat
import tempfile
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import NoReturn

from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.codex.account.auth_status import (
    probe_codex_auth_status,
)
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.executable import (
    verify_codex_executable,
)
from sidekick_usages.providers.codex.app_server.methods import (
    ACCOUNT_CHATGPT_AUTH_REFRESH_METHOD,
    ACCOUNT_LOGIN_COMPLETED_METHOD,
    ACCOUNT_LOGIN_START_METHOD,
    ACCOUNT_READ_METHOD,
    ACCOUNT_UPDATED_METHOD,
    CONFIG_READ_METHOD,
    INITIALIZE_METHOD,
    INITIALIZED_METHOD,
    MCP_SERVER_STATUS_LIST_METHOD,
    MCP_SERVER_STATUS_UPDATED_METHOD,
    MODEL_PROVIDER_CAPABILITIES_READ_METHOD,
    THREAD_REALTIME_CLOSED_METHOD,
    THREAD_REALTIME_START_METHOD,
    THREAD_REALTIME_STARTED_METHOD,
    TURN_COMPLETED_METHOD,
    TURN_START_METHOD,
    TURN_STARTED_METHOD,
)
from sidekick_usages.providers.codex.app_server.models import (
    CodexAppServerCapabilities,
    CodexExecutable,
    CodexVersion,
)
from sidekick_usages.providers.codex.app_server.process import (
    minimal_codex_environment,
    run_quiet_codex_command,
)
from sidekick_usages.providers.codex.app_server.session import (
    CodexAppServerSession,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
    CodexProcessGroupPolicy,
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
    "v2/AccountLoginCompletedNotification.json",
    "v2/AccountUpdatedNotification.json",
    "ChatgptAuthTokensRefreshParams.json",
    "ChatgptAuthTokensRefreshResponse.json",
    "ClientRequest.json",
    "ClientNotification.json",
    "ServerRequest.json",
    "ServerNotification.json",
)
_SESSION_VERSION = CodexVersion(0, 146, 0)
_SESSION_SCHEMA_FILES = (
    "v2/ConfigReadParams.json",
    "v2/ConfigReadResponse.json",
    "v2/ModelProviderCapabilitiesReadParams.json",
    "v2/ModelProviderCapabilitiesReadResponse.json",
    "v2/TurnStartParams.json",
    "v2/TurnStartedNotification.json",
    "v2/TurnCompletedNotification.json",
    "v2/ThreadRealtimeStartParams.json",
    "v2/ThreadRealtimeStartedNotification.json",
    "v2/ThreadRealtimeClosedNotification.json",
    "v2/McpServerStatusListResponse.json",
)
_MAX_SCHEMA_FILE_BYTES = 512 * 1024
_MAX_SCHEMA_DEPTH = 32
_SCHEMA_COMMAND_TIMEOUT_SECONDS = 20.0


def probe_codex_capabilities(
    executable: CodexExecutable,
    environment: Mapping[str, str] | None = None,
    *,
    process_group: CodexProcessGroupPolicy = (
        CodexProcessGroupPolicy.ISOLATED
    ),
    cancelled: Callable[[], bool] | None = None,
) -> CodexAppServerCapabilities:
    """Prove the exact generated and unexported app-server capabilities."""
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
                str(executable.provenance.path),
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
            working_directory=codex_home,
            process_group=process_group,
            cancelled=cancelled,
        )
        schema_files = _schema_files(executable.version)
        raw_schemas, schemas = _read_required_schemas(
            schema_directory,
            schema_files,
        )
        _validate_required_capabilities(schemas)
        session_schema_supported = executable.version == _SESSION_VERSION
        if session_schema_supported:
            _validate_session_capabilities(schemas)
        schema_hash = _hash_schemas(raw_schemas, schema_files)
        capabilities = CodexAppServerCapabilities(
            executable=executable,
            schema_hash=schema_hash,
            session_schema_supported=session_schema_supported,
        )
        try:
            with CodexAppServerSession.open(
                capabilities,
                codex_home,
                environment,
                process_group=process_group,
            ) as session:
                probe_codex_auth_status(session)
        except CodexAppServerError as error:
            if error.code is CodexAppServerFailure.EXECUTABLE_UNSAFE:
                raise
            raise CodexAppServerError(
                CodexAppServerFailure.CAPABILITY_UNSUPPORTED
            ) from None
        verify_codex_executable(executable)
        return capabilities


def _read_required_schemas(
    schema_directory: Path,
    schema_files: tuple[str, ...],
) -> tuple[dict[str, bytes], dict[str, JsonObject]]:
    raw_schemas: dict[str, bytes] = {}
    schemas: dict[str, JsonObject] = {}
    for relative in schema_files:
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


def _schema_files(version: CodexVersion) -> tuple[str, ...]:
    if version == _SESSION_VERSION:
        return (*SCHEMA_FILES, *_SESSION_SCHEMA_FILES)
    return SCHEMA_FILES


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
    _require_property_anywhere(
        login_params,
        "chatgptPlanType",
        "string",
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

    login_completed = schemas["v2/AccountLoginCompletedNotification.json"]
    _require_names(login_completed, "required", ("success",))
    _require_property(login_completed, "error", "string")
    _require_property(login_completed, "loginId", "string")
    _require_property(login_completed, "success", "boolean")

    account_updated = schemas["v2/AccountUpdatedNotification.json"]
    _require_property_enum(
        account_updated,
        "authMode",
        "chatgpt",
    )
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

    for method in (
        INITIALIZE_METHOD,
        ACCOUNT_LOGIN_START_METHOD,
        ACCOUNT_READ_METHOD,
    ):
        _require_method(schemas["ClientRequest.json"], method)
    _require_method(
        schemas["ClientNotification.json"],
        INITIALIZED_METHOD,
    )
    _require_method(
        schemas["ServerRequest.json"],
        ACCOUNT_CHATGPT_AUTH_REFRESH_METHOD,
    )
    _require_method(
        schemas["ServerNotification.json"],
        ACCOUNT_LOGIN_COMPLETED_METHOD,
    )
    _require_method(
        schemas["ServerNotification.json"],
        ACCOUNT_UPDATED_METHOD,
    )
    _require_property(
        schemas["ServerNotification.json"],
        "emittedAtMs",
        "integer",
    )


def _validate_session_capabilities(
    schemas: dict[str, JsonObject],
) -> None:
    config_params = schemas["v2/ConfigReadParams.json"]
    _require_property(config_params, "includeLayers", "boolean")
    config_response = schemas["v2/ConfigReadResponse.json"]
    _require_names(config_response, "required", ("config", "layers"))
    _require_property(config_response, "config", "object")
    _require_property(config_response, "layers", "array")

    provider_params = schemas[
        "v2/ModelProviderCapabilitiesReadParams.json"
    ]
    _require_names(provider_params, "required", ("modelProvider",))
    _require_property(provider_params, "modelProvider", "string")
    provider_response = schemas[
        "v2/ModelProviderCapabilitiesReadResponse.json"
    ]
    _require_names(
        provider_response,
        "required",
        ("authResolution", "modelTransport", "supportsWebsockets"),
    )
    _require_property(provider_response, "authResolution", "string")
    _require_property(provider_response, "modelTransport", "string")
    _require_property(provider_response, "supportsWebsockets", "boolean")

    _require_property(
        schemas["v2/TurnStartParams.json"],
        "threadId",
        "string",
    )
    for relative in (
        "v2/TurnStartedNotification.json",
        "v2/TurnCompletedNotification.json",
    ):
        _require_property(schemas[relative], "turn", "object")
    for relative in (
        "v2/ThreadRealtimeStartParams.json",
        "v2/ThreadRealtimeStartedNotification.json",
        "v2/ThreadRealtimeClosedNotification.json",
    ):
        _require_property(schemas[relative], "threadId", "string")
    _require_property(
        schemas["v2/McpServerStatusListResponse.json"],
        "data",
        "array",
    )

    for method in (
        CONFIG_READ_METHOD,
        MODEL_PROVIDER_CAPABILITIES_READ_METHOD,
        TURN_START_METHOD,
        THREAD_REALTIME_START_METHOD,
        MCP_SERVER_STATUS_LIST_METHOD,
    ):
        _require_method(schemas["ClientRequest.json"], method)
    for method in (
        TURN_STARTED_METHOD,
        TURN_COMPLETED_METHOD,
        THREAD_REALTIME_STARTED_METHOD,
        THREAD_REALTIME_CLOSED_METHOD,
        MCP_SERVER_STATUS_UPDATED_METHOD,
    ):
        _require_method(schemas["ServerNotification.json"], method)


def _hash_schemas(
    raw_schemas: dict[str, bytes],
    schema_files: tuple[str, ...],
) -> str:
    digest = hashlib.sha256()
    for relative in schema_files:
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

"""Protected direct-HTTP configuration for neutral Codex sessions."""

import tomllib
from pathlib import Path
from typing import NoReturn, Protocol

from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.methods import (
    CONFIG_READ_METHOD,
)
from sidekick_usages.providers.codex.app_server.models import (
    CodexVersion,
)
from sidekick_usages.providers.codex.app_server.release import (
    CODEX_SESSION_VERSION,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.session.errors import (
    CodexSessionConfigurationError,
)
from sidekick_usages.providers.codex.session.models import (
    CODEX_SESSION_BASE_URL,
    CODEX_SESSION_MODEL_PROVIDER,
    CODEX_SESSION_OPERATOR_PRECONDITION,
    CODEX_SESSION_PROVIDER_NAME,
    CODEX_SESSION_WIRE_API,
    CodexSessionCapability,
    CodexSessionConfigurationReason,
    CodexSessionPreparationReport,
)
from sidekick_usages.serialization.json import JsonObject, JsonValue

CODEX_SESSION_CONFIG_BASENAME = "config.toml"
_PROTECTED_ROOT_CONFIG = (
    "# Sidekick-owned account-selection transport.\n"
    f'model_provider = "{CODEX_SESSION_MODEL_PROVIDER}"\n'
    "\n"
).encode()
_PROTECTED_PROVIDER_CONFIG = (
    "\n[model_providers.sidekick-chatgpt-http]\n"
    f'name = "{CODEX_SESSION_PROVIDER_NAME}"\n'
    f'base_url = "{CODEX_SESSION_BASE_URL}"\n'
    f'wire_api = "{CODEX_SESSION_WIRE_API}"\n'
    "requires_openai_auth = true\n"
    "supports_websockets = false\n"
).encode()
_PROTECTED_RECOVERY = (
    CODEX_SESSION_OPERATOR_PRECONDITION,
    "Remove protected model-provider keys from user or project Codex "
    "configuration, then restart Sidekick.",
)
_STALE_RECOVERY = (
    CODEX_SESSION_OPERATOR_PRECONDITION,
    "Stop the Sidekick supervisor and neutral Codex daemon, then restart "
    "Sidekick so the protected launch configuration takes effect.",
)
_UNSAFE_CONFIG_RECOVERY = (
    CODEX_SESSION_OPERATOR_PRECONDITION,
    "Restore the neutral Codex config.toml as valid owner-only TOML, then "
    "restart Sidekick.",
)


class CodexSessionReader(Protocol):
    """Read correlated resident app-server objects."""

    def request(self, method: str, params: JsonObject) -> JsonObject:
        """Return one strict object response."""


class CodexSessionConfig:
    """Own the immutable provider overlay and effective readback."""

    def __init__(self, session_home: Path) -> None:
        if not session_home.is_absolute():
            raise ValueError("Codex session home must be absolute.")
        self._path = session_home / CODEX_SESSION_CONFIG_BASENAME

    def prepare(self, existing: bytes | None) -> bytes:
        """Return one canonical protected config preserving safe settings."""
        if existing is None:
            return _PROTECTED_ROOT_CONFIG + _PROTECTED_PROVIDER_CONFIG
        document = _decode_config(existing)
        if _protected_document(document):
            _require_protected_document(document)
            return existing
        if _document_protected_attempt(document):
            _configuration_required(
                CodexSessionConfigurationReason.PROTECTED_OVERRIDE,
                _PROTECTED_RECOVERY,
            )
        separator = b"" if existing.endswith(b"\n") else b"\n"
        prepared = (
            _PROTECTED_ROOT_CONFIG
            + existing
            + separator
            + _PROTECTED_PROVIDER_CONFIG
        )
        _require_protected_document(_decode_config(prepared))
        return prepared

    def command(self, subcommand: tuple[str, ...]) -> tuple[str, ...]:
        """Return one nonempty daemon lifecycle subcommand unchanged."""
        if not subcommand or any(not argument for argument in subcommand):
            raise ValueError("Codex session subcommand is invalid.")
        return subcommand

    def qualify(
        self,
        reader: CodexSessionReader,
        version: CodexVersion,
        *,
        session_schema_supported: bool,
    ) -> CodexSessionCapability:
        """Read and validate the resident effective model transport."""
        if version != CODEX_SESSION_VERSION or not session_schema_supported:
            raise CodexAppServerError(
                CodexAppServerFailure.CAPABILITY_UNSUPPORTED
            )
        result = reader.request(
            CONFIG_READ_METHOD,
            {"includeLayers": True},
        )
        provider, definition = _effective_provider(result, self._path)
        name = definition.get("name")
        base_url = definition.get("base_url")
        wire_api = definition.get("wire_api")
        requires_openai_auth = definition.get("requires_openai_auth")
        supports_websockets = definition.get("supports_websockets")
        if (
            not isinstance(name, str)
            or not isinstance(base_url, str)
            or not isinstance(wire_api, str)
            or not isinstance(requires_openai_auth, bool)
            or not isinstance(supports_websockets, bool)
        ):
            _malformed()
        capability = CodexSessionCapability(
            version=version,
            session_schema_supported=session_schema_supported,
            model_provider=provider,
            provider_name=name,
            base_url=base_url,
            wire_api=wire_api,
            requires_openai_auth=requires_openai_auth,
            supports_websockets=supports_websockets,
        )
        if not capability.supported:
            _configuration_required(
                CodexSessionConfigurationReason.RESIDENT_CONFIG_STALE,
                _STALE_RECOVERY,
            )
        return capability


def _decode_config(payload: bytes) -> dict[str, object]:
    try:
        return tomllib.loads(payload.decode("utf-8"))
    except UnicodeDecodeError, tomllib.TOMLDecodeError:
        _configuration_required(
            CodexSessionConfigurationReason.SESSION_CONFIG_UNSAFE,
            _UNSAFE_CONFIG_RECOVERY,
        )


def _require_protected_document(document: dict[str, object]) -> None:
    providers = _object_map(document.get("model_providers"))
    definition = (
        _object_map(providers.get(CODEX_SESSION_MODEL_PROVIDER))
        if providers is not None
        else None
    )
    if (
        document.get("model_provider") != CODEX_SESSION_MODEL_PROVIDER
        or not isinstance(definition, dict)
        or definition.get("name") != CODEX_SESSION_PROVIDER_NAME
        or definition.get("base_url") != CODEX_SESSION_BASE_URL
        or definition.get("wire_api") != CODEX_SESSION_WIRE_API
        or definition.get("requires_openai_auth") is not True
        or definition.get("supports_websockets") is not False
    ):
        _configuration_required(
            CodexSessionConfigurationReason.PROTECTED_OVERRIDE,
            _PROTECTED_RECOVERY,
        )


def _protected_document(document: dict[str, object]) -> bool:
    providers = _object_map(document.get("model_providers"))
    return document.get("model_provider") == CODEX_SESSION_MODEL_PROVIDER and (
        providers is not None and CODEX_SESSION_MODEL_PROVIDER in providers
    )


def _document_protected_attempt(document: dict[str, object]) -> bool:
    if "model_provider" in document:
        return True
    providers = _object_map(document.get("model_providers"))
    return providers is not None and (
        CODEX_SESSION_MODEL_PROVIDER in providers
    )


def _object_map(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    normalized: dict[str, object] = {}
    for name, item in value.items():
        if not isinstance(name, str):
            return None
        normalized[name] = item
    return normalized


def _effective_provider(
    result: JsonObject,
    config_path: Path,
) -> tuple[str, JsonObject]:
    if set(result) != {"config", "layers", "origins"}:
        _malformed()
    config = result.get("config")
    layers = result.get("layers")
    origins = result.get("origins")
    if (
        not isinstance(config, dict)
        or not isinstance(layers, list)
        or not isinstance(origins, dict)
    ):
        _malformed()
    provider = config.get("model_provider")
    if not isinstance(provider, str):
        _malformed()
    protected = _protected_layer_config(layers, config_path)
    _validate_origins(origins, config_path)
    providers = protected.get("model_providers")
    if protected.get("model_provider") != provider or not isinstance(
        providers,
        dict,
    ):
        _malformed()
    definition = providers.get(CODEX_SESSION_MODEL_PROVIDER)
    if not isinstance(definition, dict):
        _malformed()
    return provider, definition


def _protected_layer_config(
    layers: list[JsonValue],
    config_path: Path,
) -> JsonObject:
    """Return the exact neutral-home user layer."""
    protected: JsonObject | None = None
    for layer in layers:
        source, layer_config = _config_layer(layer)
        if _canonical_user_source(source, config_path):
            if protected is not None:
                _malformed()
            protected = layer_config
        elif _protected_attempt(layer_config):
            _configuration_required(
                CodexSessionConfigurationReason.PROTECTED_OVERRIDE,
                _PROTECTED_RECOVERY,
            )
    if protected is None:
        _malformed()
    return protected


def _validate_origins(origins: JsonObject, config_path: Path) -> None:
    """Require the effective provider to originate in the neutral home."""
    for metadata in origins.values():
        _config_metadata(metadata)
    origin = origins.get("model_provider")
    if not isinstance(origin, dict) or not _canonical_user_source(
        _config_metadata(origin),
        config_path,
    ):
        _malformed()


def _canonical_user_source(source: JsonObject, config_path: Path) -> bool:
    return source.get("type") == "user" and source.get("file") == str(
        config_path
    )


def _config_layer(value: JsonValue) -> tuple[JsonObject, JsonObject]:
    if (
        not isinstance(value, dict)
        or not {
            "config",
            "name",
            "version",
        }
        <= set(value)
        or not set(value)
        <= {
            "config",
            "disabledReason",
            "name",
            "version",
        }
    ):
        _malformed()
    config = value.get("config")
    source = value.get("name")
    version = value.get("version")
    disabled_reason = value.get("disabledReason")
    if (
        not isinstance(config, dict)
        or not isinstance(source, dict)
        or not isinstance(version, str)
        or not version
        or (
            disabled_reason is not None
            and not isinstance(disabled_reason, str)
        )
    ):
        _malformed()
    _config_source(source)
    return source, config


def _config_metadata(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict) or set(value) != {"name", "version"}:
        _malformed()
    source = value.get("name")
    version = value.get("version")
    if not isinstance(source, dict) or not isinstance(version, str):
        _malformed()
    _config_source(source)
    return source


def _config_source(source: JsonObject) -> None:
    source_type = source.get("type")
    expected_fields = {
        "mdm": {"domain", "key", "type"},
        "system": {"file", "type"},
        "enterpriseManaged": {"id", "name", "type"},
        "user": {"file", "type"},
        "project": {"dotCodexFolder", "type"},
        "sessionFlags": {"type"},
        "legacyManagedConfigTomlFromFile": {"file", "type"},
        "legacyManagedConfigTomlFromMdm": {"type"},
    }
    if not isinstance(source_type, str):
        _malformed()
    fields = expected_fields.get(source_type)
    actual = set(source)
    if source_type == "user" and "profile" in actual:
        profile = source.get("profile")
        if profile is not None and not isinstance(profile, str):
            _malformed()
        actual.remove("profile")
    if (
        fields is None
        or actual != fields
        or any(
            not isinstance(value, str)
            for name, value in source.items()
            if name != "profile"
        )
    ):
        _malformed()


def _protected_attempt(config: JsonObject) -> bool:
    if "model_provider" in config:
        return True
    providers = config.get("model_providers")
    if not isinstance(providers, dict):
        return False
    return CODEX_SESSION_MODEL_PROVIDER in providers


def _configuration_required(
    reason: CodexSessionConfigurationReason,
    operator_steps: tuple[str, ...],
) -> NoReturn:
    raise CodexSessionConfigurationError(
        CodexSessionPreparationReport(reason, operator_steps)
    )


def _malformed() -> NoReturn:
    raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)

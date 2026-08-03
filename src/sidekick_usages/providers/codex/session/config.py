"""Protected direct-HTTP configuration for neutral Codex sessions."""

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

_PROTECTED_ARGUMENTS = (
    "-c",
    f'model_provider="{CODEX_SESSION_MODEL_PROVIDER}"',
    "-c",
    (
        "model_providers.sidekick-chatgpt-http.name="
        f'"{CODEX_SESSION_PROVIDER_NAME}"'
    ),
    "-c",
    (
        "model_providers.sidekick-chatgpt-http.base_url="
        f'"{CODEX_SESSION_BASE_URL}"'
    ),
    "-c",
    (
        "model_providers.sidekick-chatgpt-http.wire_api="
        f'"{CODEX_SESSION_WIRE_API}"'
    ),
    "-c",
    ("model_providers.sidekick-chatgpt-http.requires_openai_auth=true"),
    "-c",
    ("model_providers.sidekick-chatgpt-http.supports_websockets=false"),
)
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


class CodexSessionReader(Protocol):
    """Read correlated resident app-server objects."""

    def request(self, method: str, params: JsonObject) -> JsonObject:
        """Return one strict object response."""


class CodexSessionConfig:
    """Own the immutable provider overlay and effective readback."""

    @property
    def arguments(self) -> tuple[str, ...]:
        """Return the exact global CLI override tuple."""
        return _PROTECTED_ARGUMENTS

    def command(self, subcommand: tuple[str, ...]) -> tuple[str, ...]:
        """Prefix one nonempty Codex subcommand with protected overrides."""
        if not subcommand or any(not argument for argument in subcommand):
            raise ValueError("Codex session subcommand is invalid.")
        return (*self.arguments, *subcommand)

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
        provider, definition = _effective_provider(result)
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


def _effective_provider(result: JsonObject) -> tuple[str, JsonObject]:
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
    session_flags = _session_flag_config(layers)
    _validate_origins(origins)
    providers = session_flags.get("model_providers")
    if session_flags.get("model_provider") != provider or not isinstance(
        providers,
        dict,
    ):
        _malformed()
    definition = providers.get(CODEX_SESSION_MODEL_PROVIDER)
    if not isinstance(definition, dict):
        _malformed()
    return provider, definition


def _session_flag_config(layers: list[JsonValue]) -> JsonObject:
    """Return the single typed session-flags layer."""
    session_flags: JsonObject | None = None
    for layer in layers:
        source, layer_config = _config_layer(layer)
        source_type = source.get("type")
        if source_type in {"user", "project"} and _protected_attempt(
            layer_config
        ):
            _configuration_required(
                CodexSessionConfigurationReason.PROTECTED_OVERRIDE,
                _PROTECTED_RECOVERY,
            )
        if source_type == "sessionFlags":
            if session_flags is not None:
                _malformed()
            session_flags = layer_config
    if session_flags is None:
        _malformed()
    return session_flags


def _validate_origins(origins: JsonObject) -> None:
    """Require the effective provider to originate in session flags."""
    for metadata in origins.values():
        _config_metadata(metadata)
    origin = origins.get("model_provider")
    if (
        not isinstance(origin, dict)
        or _config_metadata(origin).get("type") != "sessionFlags"
    ):
        _malformed()


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

"""Immutable Codex interactive-session capability."""

from dataclasses import dataclass

from sidekick_usages.providers.codex.app_server.models import CodexVersion

SUPPORTED_CODEX_SESSION_VERSION = CodexVersion(0, 146, 0)
CODEX_SESSION_MODEL_PROVIDER = "sidekick-chatgpt-http"
CODEX_SESSION_PROVIDER_NAME = "OpenAI"
CODEX_SESSION_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_SESSION_WIRE_API = "responses"
CODEX_SESSION_MODEL_TRANSPORT = "http"
CODEX_SESSION_AUTH_RESOLUTION = "perAttempt"


@dataclass(frozen=True, slots=True)
class CodexSessionCapability:
    """Effective direct-HTTP capability for one resident session."""

    version: CodexVersion
    session_schema_supported: bool
    model_provider: str
    provider_name: str
    base_url: str
    wire_api: str
    requires_openai_auth: bool
    supports_websockets: bool
    model_transport: str
    auth_resolution: str

    @property
    def supported(self) -> bool:
        """Return whether all exact interactive-session contracts hold."""
        return (
            self.version == SUPPORTED_CODEX_SESSION_VERSION
            and self.session_schema_supported
            and self.model_provider == CODEX_SESSION_MODEL_PROVIDER
            and self.provider_name == CODEX_SESSION_PROVIDER_NAME
            and self.base_url == CODEX_SESSION_BASE_URL
            and self.wire_api == CODEX_SESSION_WIRE_API
            and self.requires_openai_auth
            and not self.supports_websockets
            and self.model_transport == CODEX_SESSION_MODEL_TRANSPORT
            and self.auth_resolution == CODEX_SESSION_AUTH_RESOLUTION
        )

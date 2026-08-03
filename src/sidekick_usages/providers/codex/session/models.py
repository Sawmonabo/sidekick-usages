"""Immutable Codex interactive-session capability."""

from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.core.recovery import PreparationReport
from sidekick_usages.providers.codex.app_server.models import CodexVersion
from sidekick_usages.providers.codex.app_server.release import (
    CODEX_SESSION_VERSION,
)

CODEX_SESSION_MODEL_PROVIDER = "sidekick-chatgpt-http"
CODEX_SESSION_PROVIDER_NAME = "OpenAI"
CODEX_SESSION_BASE_URL = "https://chatgpt.com/backend-api/codex"
CODEX_SESSION_WIRE_API = "responses"
CODEX_SESSION_OPERATOR_PRECONDITION = (
    "Out of band, confirm no integrated Codex session is active; do not use "
    "account selection as session preparation."
)


class CodexSessionConfigurationReason(StrEnum):
    """Closed reasons a neutral session requires operator preparation."""

    HOME_UNSAFE = "home_unsafe"
    CREDENTIAL_STATE_PRESENT = "credential_state_present"
    PRIVATE_AUTHORITY_COLLISION = "private_authority_collision"
    PROTECTED_OVERRIDE = "protected_override"
    RESIDENT_CONFIG_STALE = "resident_config_stale"


class CodexSessionPreparationReport(
    PreparationReport[CodexSessionConfigurationReason]
):
    """Bounded token-free dry-run detail for operator recovery."""

    def __post_init__(self) -> None:
        """Require one exact Codex reason and shared bounded sequence."""
        if not isinstance(self.reason, CodexSessionConfigurationReason):
            raise ValueError("Codex session preparation report is invalid.")
        super().__post_init__()


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

    @property
    def supported(self) -> bool:
        """Return whether all exact interactive-session contracts hold."""
        return (
            self.version == CODEX_SESSION_VERSION
            and self.session_schema_supported
            and self.model_provider == CODEX_SESSION_MODEL_PROVIDER
            and self.provider_name == CODEX_SESSION_PROVIDER_NAME
            and self.base_url == CODEX_SESSION_BASE_URL
            and self.wire_api == CODEX_SESSION_WIRE_API
            and self.requires_openai_auth
            and not self.supports_websockets
        )

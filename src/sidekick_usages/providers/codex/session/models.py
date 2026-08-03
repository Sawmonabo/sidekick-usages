"""Immutable Codex interactive-session capability."""

from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    SidekickAccountId,
)
from sidekick_usages.core.recovery import PreparationReport
from sidekick_usages.core.selection.models import SelectionEpoch
from sidekick_usages.core.selection.types import TurnId
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
    MANAGED_INSTALL_UNAVAILABLE = "managed_install_unavailable"
    PRIVATE_AUTHORITY_COLLISION = "private_authority_collision"
    PROTECTED_OVERRIDE = "protected_override"
    RESIDENT_CONFIG_STALE = "resident_config_stale"
    SESSION_CONFIG_UNSAFE = "session_config_unsafe"


class CodexRelayAdmissionState(StrEnum):
    """Closed admission outcomes returned to one provider relay."""

    ADMITTED = "admitted"
    QUEUED = "queued"


class CodexRelayLeaseKind(StrEnum):
    """Closed account-bearing lease kinds observed by the relay."""

    TURN = "turn"
    REALTIME = "realtime"


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexRelayAuthority:
    """Exact selected authority bound to one admitted relay turn."""

    account_id: SidekickAccountId
    generation: AuthorityGeneration
    epoch: SelectionEpoch


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexLoadedThreadSnapshot:
    """Versioned deterministic loaded-thread readiness input."""

    revision: int
    thread_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """Require one monotonic revision for sorted unique IDs."""
        if (
            isinstance(self.revision, bool)
            or self.revision < 0
            or self.revision != len(self.thread_ids)
            or self.thread_ids != tuple(sorted(set(self.thread_ids)))
        ):
            raise ValueError("Codex loaded-thread snapshot is invalid.")


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexRelayAdmission:
    """One stable relay turn admitted now or retained for later."""

    turn_id: TurnId
    state: CodexRelayAdmissionState
    authority: CodexRelayAuthority | None

    def __post_init__(self) -> None:
        """Require exact authority only for an admitted relay turn."""
        if (self.state is CodexRelayAdmissionState.ADMITTED) != (
            self.authority is not None
        ):
            raise ValueError("Codex relay admission is inconsistent.")


@dataclass(frozen=True, slots=True, kw_only=True)
class CodexRelayLease:
    """Safe routing correlation for one admitted provider operation."""

    turn_id: TurnId
    kind: CodexRelayLeaseKind
    request_id: int | str
    thread_id: str


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

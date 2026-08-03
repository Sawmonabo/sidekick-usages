"""Secret-free resident-service lifecycle models."""

from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.core.selection.models import safe_outcome_code
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.daemon.types.lifecycle import (
    ServiceBackendId,
    ServiceComponentState,
    ServiceFailureCode,
    ServiceLifecyclePhase,
    ServiceLifecycleState,
)
from sidekick_usages.daemon.types.service import PackageVersion

_MAX_ARTIFACT_BYTES = 256 * 1024
_MAX_IDENTITY_BYTES = 256
_MAX_MESSAGE_BYTES = 1024
_SERVICE_ARTIFACT_MODE = 0o600
MAX_COMMAND_OUTPUT_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Completed bounded native command result."""

    returncode: int
    stdout: str
    stderr: str

    def __post_init__(self) -> None:
        """Require bounded native output and an integer process result."""
        if type(self.returncode) is not int:
            raise ValueError("Command return code must be an integer.")
        _require_text_bound(
            self.stdout,
            "Command standard output",
            MAX_COMMAND_OUTPUT_BYTES,
            allow_empty=True,
        )
        _require_text_bound(
            self.stderr,
            "Command standard error",
            MAX_COMMAND_OUTPUT_BYTES,
            allow_empty=True,
        )


@dataclass(frozen=True, slots=True)
class PlatformInfo:
    """Platform facts used to select one user-service integration."""

    system: str
    home: Path
    uid: int
    user_name: str
    is_wsl: bool
    wsl_distro: str | None
    has_user_systemd: bool

    def __post_init__(self) -> None:
        """Validate exact local identities without guessing WSL values."""
        if not self.home.is_absolute():
            raise ValueError("Platform home must be absolute.")
        if self.uid < 0:
            raise ValueError("Platform user ID cannot be negative.")
        _require_identity(self.system, "Platform system")
        _require_identity(self.user_name, "Platform user name")
        if self.is_wsl:
            if self.system != "Linux":
                raise ValueError("WSL requires Linux.")
            if self.wsl_distro is not None:
                _require_identity(self.wsl_distro, "WSL distribution")
        elif self.wsl_distro is not None:
            raise ValueError("Non-WSL platforms cannot name a distribution.")


@dataclass(frozen=True, slots=True)
class ServiceArtifact:
    """One exact generated user-service definition."""

    path: Path
    payload: bytes
    mode: int = _SERVICE_ARTIFACT_MODE

    def __post_init__(self) -> None:
        """Require one bounded absolute owner-only artifact."""
        if not self.path.is_absolute():
            raise ValueError("Service artifact path must be absolute.")
        if not self.payload or len(self.payload) > _MAX_ARTIFACT_BYTES:
            raise ValueError("Service artifact payload is outside its bound.")
        if self.mode != _SERVICE_ARTIFACT_MODE:
            raise ValueError("Service artifacts must be owner-only.")


@dataclass(frozen=True, slots=True)
class ServiceLaunchCommand:
    """One exact qualified supervisor command."""

    program: Path
    arguments: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject ambiguous programs and unsafe native arguments."""
        values = (str(self.program), *self.arguments)
        if not self.program.is_absolute() or any(
            not value or "\0" in value or "\n" in value or "\r" in value
            for value in values
        ):
            raise ValueError("Service launch command is invalid.")


@dataclass(frozen=True, slots=True)
class ServiceBackendStatus:
    """Read-only aggregate and independent platform component state."""

    backend: ServiceBackendId
    state: ServiceLifecycleState
    process: ServiceComponentState
    rescue: ServiceComponentState

    def __post_init__(self) -> None:
        """Require closed backend and lifecycle values."""
        if not isinstance(self.backend, ServiceBackendId) or not isinstance(
            self.state,
            ServiceLifecycleState,
        ):
            raise ValueError("Service status values are invalid.")
        process_valid = isinstance(self.process, ServiceComponentState)
        rescue_valid = isinstance(self.rescue, ServiceComponentState)
        if not process_valid or not rescue_valid:
            raise ValueError("Service component status values are invalid.")
        if (
            self.backend is ServiceBackendId.WSL
            and self.rescue is ServiceComponentState.NOT_REQUIRED
        ):
            raise ValueError("WSL status requires explicit rescue state.")

    @classmethod
    def single(
        cls,
        backend: ServiceBackendId,
        state: ServiceLifecycleState,
    ) -> ServiceBackendStatus:
        """Build a backend without an independent rescue component."""
        return cls(
            backend,
            state,
            process_component_state(state),
            ServiceComponentState.NOT_REQUIRED,
        )

    @classmethod
    def observation_failed(
        cls,
        backend: ServiceBackendId,
    ) -> ServiceBackendStatus:
        """Build truthful component health after backend observation fails."""
        rescue = (
            ServiceComponentState.UNHEALTHY
            if backend is ServiceBackendId.WSL
            else ServiceComponentState.NOT_REQUIRED
        )
        return cls(
            backend,
            ServiceLifecycleState.UNHEALTHY,
            ServiceComponentState.UNHEALTHY,
            rescue,
        )


@dataclass(frozen=True, slots=True)
class ServiceLifecycleObservation:
    """One secret-free transient lifecycle progress observation."""

    phase: ServiceLifecyclePhase
    provider_id: ProviderId | None = None

    def __post_init__(self) -> None:
        """Require provider identity only for its capability proof."""
        if not isinstance(self.phase, ServiceLifecyclePhase):
            raise ValueError("Service lifecycle progress phase is invalid.")
        provider_capability = (
            self.phase is ServiceLifecyclePhase.PROVIDER_CAPABILITY
        )
        if provider_capability != (self.provider_id is not None):
            raise ValueError("Service lifecycle progress provider is invalid.")
        if self.provider_id is not None and not isinstance(
            self.provider_id,
            ProviderId,
        ):
            raise ValueError("Service lifecycle progress provider is invalid.")


@dataclass(frozen=True, slots=True, kw_only=True)
class SupervisorHealth:
    """Independent secret-free health for the resident supervisor."""

    backend: ServiceBackendId
    cli_version: PackageVersion
    supervisor_version: PackageVersion | None
    platform: ServiceComponentState
    process: ServiceComponentState
    rescue: ServiceComponentState
    socket: ServiceComponentState
    peer: ServiceComponentState
    protocol: ServiceComponentState
    queue: ServiceComponentState
    journal: ServiceComponentState
    broker: ServiceComponentState
    broker_failure_code: str | None = None

    def __post_init__(self) -> None:
        """Require closed component states and bounded versions."""
        if not isinstance(self.backend, ServiceBackendId):
            raise ValueError("Supervisor backend is invalid.")
        if not isinstance(self.cli_version, PackageVersion):
            raise ValueError("CLI package version is invalid.")
        if self.supervisor_version is not None and not isinstance(
            self.supervisor_version,
            PackageVersion,
        ):
            raise ValueError("Supervisor package version is invalid.")
        components = (
            self.platform,
            self.process,
            self.rescue,
            self.socket,
            self.peer,
            self.protocol,
            self.queue,
            self.journal,
            self.broker,
        )
        if not all(
            isinstance(component, ServiceComponentState)
            for component in components
        ):
            raise ValueError("Supervisor component health is invalid.")
        broker_failure_code = safe_outcome_code(self.broker_failure_code)
        if (
            broker_failure_code is not None
            and self.broker is not ServiceComponentState.UNHEALTHY
        ):
            raise ValueError(
                "Supervisor broker failure requires unhealthy broker state."
            )
        object.__setattr__(
            self,
            "broker_failure_code",
            broker_failure_code,
        )


@dataclass(frozen=True, slots=True)
class DaemonOperationResult:
    """Safe result returned by a public daemon lifecycle command."""

    backend: ServiceBackendId
    state: ServiceLifecycleState
    message: str
    exit_code: ExitCode = ExitCode.SUCCESS
    failure_code: ServiceFailureCode | None = None
    failure_provider_id: ProviderId | None = None

    def __post_init__(self) -> None:
        """Require safe bounded presentation state."""
        if not isinstance(self.backend, ServiceBackendId) or not isinstance(
            self.state,
            ServiceLifecycleState,
        ):
            raise ValueError("Daemon result state is invalid.")
        if not isinstance(self.exit_code, ExitCode):
            raise ValueError("Daemon exit code is invalid.")
        if self.failure_code is not None and (
            not isinstance(self.failure_code, ServiceFailureCode)
            or self.state is not ServiceLifecycleState.UNHEALTHY
        ):
            raise ValueError("Daemon failure code is invalid.")
        provider_failure = (
            self.failure_code
            is ServiceFailureCode.PROVIDER_CAPABILITY_UNAVAILABLE
        )
        if provider_failure != (self.failure_provider_id is not None):
            raise ValueError("Daemon provider failure identity is invalid.")
        if self.failure_provider_id is not None and not isinstance(
            self.failure_provider_id,
            ProviderId,
        ):
            raise ValueError("Daemon provider failure identity is invalid.")
        _require_text_bound(
            self.message,
            "Daemon result message",
            _MAX_MESSAGE_BYTES,
        )


def _require_identity(value: str, name: str) -> None:
    _require_text_bound(value, name, _MAX_IDENTITY_BYTES)
    if "\0" in value or "\n" in value or "\r" in value:
        raise ValueError(f"{name} is invalid.")


def process_component_state(
    state: ServiceLifecycleState,
) -> ServiceComponentState:
    """Map native lifecycle state to resident-process health."""
    if state is ServiceLifecycleState.READY:
        return ServiceComponentState.HEALTHY
    if state is ServiceLifecycleState.ABSENT:
        return ServiceComponentState.ABSENT
    if state is ServiceLifecycleState.FEATURE_DISABLED:
        return ServiceComponentState.FEATURE_DISABLED
    return ServiceComponentState.UNHEALTHY


def _require_text_bound(
    value: str,
    name: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{name} must be valid UTF-8.") from None
    if (not encoded and not allow_empty) or len(encoded) > maximum:
        raise ValueError(f"{name} is outside its size bound.")

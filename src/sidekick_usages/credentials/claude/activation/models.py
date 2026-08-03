"""Secret-safe Claude activation runtime and failures."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sidekick_usages.core.accounts.models import (
    ClaudeManagedLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.selection.models import ClaudeAuthObservation
from sidekick_usages.core.selection.types import ProviderAuthState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.activation.foreground import (
    inspect_claude_foreground,
    inspect_claude_remote_control,
)
from sidekick_usages.providers.claude.activation.types import (
    CLAUDE_ACTIVATION_FAILURE_CODE_PREFIX,
    ClaudeActivationGuardFailure,
    ClaudeForegroundProbe,
    ClaudeRemoteControlProbe,
)
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import ClaudeCommandRunner


class ClaudeActivationFailure(StrEnum):
    """Closed safe failures from native Claude activation."""

    INCOMPATIBLE = "incompatible"
    NATIVE_CHANGED = "native_changed"
    NATIVE_UNAVAILABLE = "native_unavailable"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    SOURCE_UNAVAILABLE = "source_unavailable"
    STATE_CHANGED = "state_changed"
    TARGET_UNAVAILABLE = "target_unavailable"
    TIMED_OUT = "timed_out"

    @property
    def action_required(self) -> bool:
        """Return whether activation requires user repair."""
        return self in {
            ClaudeActivationFailure.INCOMPATIBLE,
            ClaudeActivationFailure.NATIVE_CHANGED,
            ClaudeActivationFailure.NATIVE_UNAVAILABLE,
            ClaudeActivationFailure.RECONCILIATION_REQUIRED,
            ClaudeActivationFailure.SOURCE_UNAVAILABLE,
            ClaudeActivationFailure.TARGET_UNAVAILABLE,
        }

    @property
    def failure_code(self) -> str:
        """Return the complete sanitized worker and journal code."""
        return CLAUDE_ACTIVATION_FAILURE_CODE_PREFIX + self.value


class ClaudeActivationError(RuntimeError):
    """One secret-safe native Claude activation failure."""

    def __init__(
        self,
        failure: ClaudeActivationFailure | ClaudeActivationGuardFailure,
    ) -> None:
        self.failure = failure
        super().__init__(failure.value)

    @property
    def action_required(self) -> bool:
        """Return whether the user must repair this activation."""
        return self.failure.action_required

    @property
    def timed_out(self) -> bool:
        """Return whether the official provider operation timed out."""
        return self.failure is ClaudeActivationFailure.TIMED_OUT

    @property
    def failure_code(self) -> str:
        """Return the safe worker and journal outcome code."""
        return self.failure.failure_code


@dataclass(frozen=True, slots=True)
class ClaudeActivationRuntime:
    """Injectable host and provider boundaries for one activation worker."""

    environment: Mapping[str, str] | None = None
    host: HostPlatform | None = None
    runner: ClaudeCommandRunner = run_bounded_claude_command
    foreground_probe: ClaudeForegroundProbe = inspect_claude_foreground
    remote_control_probe: ClaudeRemoteControlProbe = (
        inspect_claude_remote_control
    )


@dataclass(frozen=True, slots=True)
class ClaudeNativeObservation:
    """One secret-free strict native Claude storage observation."""

    state: ProviderAuthState
    snapshot: ClaudeAuthoritySnapshot | None = None

    def __post_init__(self) -> None:
        """Require one complete snapshot exactly for active state."""
        if self.state is not ProviderAuthState.ACTIVE:
            if self.snapshot is not None:
                raise ValueError(
                    "Inactive Claude observation claims identity."
                )
            return
        if self.snapshot is None:
            raise ValueError("Native Claude observation is incomplete.")


def claude_auth_observation(
    snapshot: ClaudeAuthoritySnapshot,
    observed_at: datetime,
) -> ClaudeAuthObservation:
    """Project one complete native proof into durable journal state."""
    return ClaudeAuthObservation(
        provider_id=ProviderId.CLAUDE,
        state=ProviderAuthState.ACTIVE,
        provider_identity=snapshot.provider_identity,
        generation=snapshot.generation,
        observed_at=observed_at,
        plan=snapshot.plan,
        scopes=snapshot.scopes,
        access_expires_at=snapshot.access_expires_at,
        refresh_expires_at=snapshot.refresh_expires_at,
        health=snapshot.health,
        action=snapshot.action,
        modified_milliseconds=snapshot.modified_milliseconds,
    )


@dataclass(frozen=True, slots=True)
class ClaudeActivationRecoveryContext:
    """Verified private and native boundaries for one recovery decision."""

    source: SavedAccount
    source_authority: ClaudeManagedLoginAuthority
    source_capabilities: ClaudeCapabilities
    source_private: ClaudeAuthoritySnapshot
    target: SavedAccount
    target_authority: ClaudeManagedLoginAuthority
    target_capabilities: ClaudeCapabilities
    target_private: ClaudeAuthoritySnapshot
    native_capabilities: ClaudeCapabilities

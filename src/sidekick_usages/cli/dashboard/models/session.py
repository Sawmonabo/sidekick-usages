"""Immutable state for one interactive dashboard process."""

from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.cli.dashboard.models.controller import (
    ActivateOrRepairIntent,
    DashboardControllerState,
    DashboardIntent,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.models import (
    DashboardFooter,
    DashboardSnapshot,
)


class DashboardConfirmationKind(StrEnum):
    """Closed approvals accepted by the interactive dashboard."""

    SERVICE_SETUP = "service_setup"
    REMOTE_CONTROL = "remote_control"


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardConfirmation:
    """One pending approval without provider-owned identity."""

    kind: DashboardConfirmationKind


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardSessionView:
    """One atomically replaceable render and interaction state."""

    snapshot: DashboardSnapshot
    controller: DashboardControllerState
    footer: DashboardFooter
    action_in_flight: bool = False
    activation_in_flight: bool = False
    confirmation: DashboardConfirmation | None = None

    def __post_init__(self) -> None:
        """Require activation and confirmation to belong to one action."""
        if self.activation_in_flight and not self.action_in_flight:
            raise ValueError("Activation requires an in-flight action.")
        if self.confirmation is not None and not self.action_in_flight:
            raise ValueError("Confirmation requires an in-flight action.")
        if (
            self.confirmation is not None
            and self.confirmation.kind
            is DashboardConfirmationKind.REMOTE_CONTROL
            and not self.activation_in_flight
        ):
            raise ValueError(
                "Remote Control confirmation requires an activation."
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardActionRequest:
    """One queued mutation plus its explicit disruption approval."""

    intent: DashboardIntent
    allow_remote_control_disconnect: bool = False

    def __post_init__(self) -> None:
        """Restrict disruption approval to an exact Claude activation."""
        if self.allow_remote_control_disconnect and (
            not isinstance(self.intent, ActivateOrRepairIntent)
            or self.intent.provider_id is not ProviderId.CLAUDE
        ):
            raise ValueError(
                "Remote Control approval requires Claude activation."
            )

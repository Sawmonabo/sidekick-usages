"""Immutable state for one interactive dashboard process."""

from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.cli.dashboard.models.controller import (
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


class DashboardStartupReconciliationState(StrEnum):
    """Closed outcomes for one provider's passive startup read-back."""

    VERIFIED = "verified"
    RETRYING = "retrying"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DashboardStartupReconciliation:
    """One provider-scoped passive startup read-back result."""

    provider_id: ProviderId
    state: DashboardStartupReconciliationState


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
    selection_in_flight: bool = False
    confirmation: DashboardConfirmation | None = None

    def __post_init__(self) -> None:
        """Require selection and confirmation to belong to one action."""
        if self.selection_in_flight and not self.action_in_flight:
            raise ValueError("Selection requires an in-flight action.")
        if self.confirmation is not None and not self.action_in_flight:
            raise ValueError("Confirmation requires an in-flight action.")


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardActionRequest:
    """One queued dashboard mutation."""

    intent: DashboardIntent

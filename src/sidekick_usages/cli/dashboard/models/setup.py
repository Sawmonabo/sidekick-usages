"""Closed guided service-setup state."""

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never


class ServiceSetupDecision(StrEnum):
    """One explicit answer to a guided installation request."""

    NOT_REQUESTED = "not_requested"
    APPROVED = "approved"
    REFUSED = "refused"


class ServiceSetupOutcome(StrEnum):
    """One terminal result from preparing the user service."""

    RESUME = "resume"
    CONFIRMATION_REQUIRED = "confirmation_required"
    REFUSED = "refused"
    FAILED = "failed"
    NONINTERACTIVE = "noninteractive"
    UNSUPPORTED = "unsupported"


class ServiceSetupProgress(StrEnum):
    """Sanitized progress safe for the dashboard footer."""

    CHECKING = "Checking the Sidekick user service."
    RESTARTING = "Restarting the Sidekick user service."
    INSTALLING = "Installing the Sidekick user service."
    READY = "The Sidekick user service is ready."


class ServiceSetupMessage(StrEnum):
    """Sanitized guidance safe for dashboard presentation."""

    READY = "The Sidekick user service is ready."
    CONFIRMATION_REQUIRED = (
        "Sidekick needs one per-user service to maintain accounts and "
        "update supported sessions. It installs without administrator "
        "access."
    )
    REFUSED = "The Sidekick user service was not installed."
    FAILED = "The Sidekick user service could not be made ready."
    NONINTERACTIVE = "The Sidekick user service requires interactive setup."
    UNSUPPORTED = "Account switching is not supported on native Windows."


class ServiceSetupAction(StrEnum):
    """One exact corrective action for a blocked setup."""

    OPEN_DASHBOARD = (
        "Run sidekick-usages in a terminal and approve service setup."
    )
    RETRY_DASHBOARD = (
        "Retry in sidekick-usages; run sidekick-usages daemon status "
        "if setup fails again."
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceSetupResult[IntentT]:
    """Return one original intent with its guided-setup disposition."""

    intent: IntentT
    outcome: ServiceSetupOutcome

    @property
    def message(self) -> ServiceSetupMessage:
        """Return the one safe message for this outcome."""
        match self.outcome:
            case ServiceSetupOutcome.RESUME:
                return ServiceSetupMessage.READY
            case ServiceSetupOutcome.CONFIRMATION_REQUIRED:
                return ServiceSetupMessage.CONFIRMATION_REQUIRED
            case ServiceSetupOutcome.REFUSED:
                return ServiceSetupMessage.REFUSED
            case ServiceSetupOutcome.FAILED:
                return ServiceSetupMessage.FAILED
            case ServiceSetupOutcome.NONINTERACTIVE:
                return ServiceSetupMessage.NONINTERACTIVE
            case ServiceSetupOutcome.UNSUPPORTED:
                return ServiceSetupMessage.UNSUPPORTED
        assert_never(self.outcome)

    @property
    def corrective_action(self) -> ServiceSetupAction | None:
        """Return one corrective action only for recoverable blocks."""
        match self.outcome:
            case ServiceSetupOutcome.REFUSED:
                return ServiceSetupAction.OPEN_DASHBOARD
            case ServiceSetupOutcome.FAILED:
                return ServiceSetupAction.RETRY_DASHBOARD
            case ServiceSetupOutcome.NONINTERACTIVE:
                return ServiceSetupAction.OPEN_DASHBOARD
            case (
                ServiceSetupOutcome.RESUME
                | ServiceSetupOutcome.CONFIRMATION_REQUIRED
                | ServiceSetupOutcome.UNSUPPORTED
            ):
                return None
        assert_never(self.outcome)

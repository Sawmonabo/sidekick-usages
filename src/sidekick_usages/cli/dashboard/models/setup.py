"""Closed guided service-setup state."""

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from sidekick_usages.core.types import ProviderId


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
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NONINTERACTIVE = "noninteractive"
    UNSUPPORTED = "unsupported"


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
    CLAUDE_UNAVAILABLE = (
        "Sidekick could not verify the required Claude CLI capabilities."
    )
    CODEX_UNAVAILABLE = (
        "Sidekick could not verify the required Codex CLI capabilities."
    )
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
    CHECK_CLAUDE = "Run sidekick-usages doctor --provider claude."
    CHECK_CODEX = "Run sidekick-usages doctor --provider codex."


@dataclass(frozen=True, slots=True, kw_only=True)
class ServiceSetupResult[IntentT]:
    """Return one original intent with its guided-setup disposition."""

    intent: IntentT
    outcome: ServiceSetupOutcome
    provider_id: ProviderId | None = None

    def __post_init__(self) -> None:
        """Require a provider only for provider-capability failure."""
        provider_failure = (
            self.outcome is ServiceSetupOutcome.PROVIDER_UNAVAILABLE
        )
        if provider_failure != (self.provider_id is not None):
            raise ValueError("Service setup provider failure is invalid.")

    @property
    def message(self) -> ServiceSetupMessage:
        """Return the one safe message for this outcome."""
        match self.outcome:
            case ServiceSetupOutcome.RESUME:
                message = ServiceSetupMessage.READY
            case ServiceSetupOutcome.CONFIRMATION_REQUIRED:
                message = ServiceSetupMessage.CONFIRMATION_REQUIRED
            case ServiceSetupOutcome.REFUSED:
                message = ServiceSetupMessage.REFUSED
            case ServiceSetupOutcome.FAILED:
                message = ServiceSetupMessage.FAILED
            case ServiceSetupOutcome.PROVIDER_UNAVAILABLE:
                message = self._provider_message
            case ServiceSetupOutcome.NONINTERACTIVE:
                message = ServiceSetupMessage.NONINTERACTIVE
            case ServiceSetupOutcome.UNSUPPORTED:
                message = ServiceSetupMessage.UNSUPPORTED
            case _:
                assert_never(self.outcome)
        return message

    @property
    def corrective_action(self) -> ServiceSetupAction | None:
        """Return one corrective action only for recoverable blocks."""
        match self.outcome:
            case ServiceSetupOutcome.REFUSED:
                return ServiceSetupAction.OPEN_DASHBOARD
            case ServiceSetupOutcome.FAILED:
                return ServiceSetupAction.RETRY_DASHBOARD
            case ServiceSetupOutcome.PROVIDER_UNAVAILABLE:
                return self._provider_action
            case ServiceSetupOutcome.NONINTERACTIVE:
                return ServiceSetupAction.OPEN_DASHBOARD
            case (
                ServiceSetupOutcome.RESUME
                | ServiceSetupOutcome.CONFIRMATION_REQUIRED
                | ServiceSetupOutcome.UNSUPPORTED
            ):
                return None
        assert_never(self.outcome)

    @property
    def _provider_message(self) -> ServiceSetupMessage:
        match self.provider_id:
            case ProviderId.CLAUDE:
                return ServiceSetupMessage.CLAUDE_UNAVAILABLE
            case ProviderId.CODEX:
                return ServiceSetupMessage.CODEX_UNAVAILABLE
            case None:
                raise AssertionError("Provider failure lost its provider.")
            case _:
                assert_never(self.provider_id)

    @property
    def _provider_action(self) -> ServiceSetupAction:
        match self.provider_id:
            case ProviderId.CLAUDE:
                return ServiceSetupAction.CHECK_CLAUDE
            case ProviderId.CODEX:
                return ServiceSetupAction.CHECK_CODEX
            case None:
                raise AssertionError("Provider failure lost its provider.")
            case _:
                assert_never(self.provider_id)

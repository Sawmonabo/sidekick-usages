"""Pre-provider commands for one coordinated Claude host."""

from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.cli.contexts.use import UseContext
from sidekick_usages.cli.dashboard.models.use import UseSelectionFailure
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId

_LOGIN_COMMAND = "/login"
_LOGOUT_COMMAND = "/logout"


class ClaudeCommandKind(StrEnum):
    """Closed routes for credential-sensitive Claude commands."""

    PROVIDER = "provider"
    SAVED_LOGIN = "saved_login"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class ClaudeCommandRoute:
    """One pre-provider command decision and visible guidance."""

    kind: ClaudeCommandKind
    guidance: str | None = None


class ClaudeSavedAccountCommands:
    """Route Claude auth commands through the existing saved-account owner."""

    def __init__(self, use: UseContext) -> None:
        self._use = use

    @property
    def accounts(self) -> tuple[SavedAccount, ...]:
        """Return saved Claude accounts in deterministic persisted order."""
        return tuple(
            account
            for account in self._use.accounts
            if account.provider_id is ProviderId.CLAUDE
        )

    def route(self, prompt: str) -> ClaudeCommandRoute:
        """Intercept only credential lifecycle commands before Claude."""
        command = prompt.strip()
        if command == _LOGIN_COMMAND:
            return ClaudeCommandRoute(ClaudeCommandKind.SAVED_LOGIN)
        first = command.split(maxsplit=1)[0].casefold() if command else ""
        if first in {_LOGIN_COMMAND, _LOGOUT_COMMAND}:
            return ClaudeCommandRoute(
                ClaudeCommandKind.REFUSED,
                "Use Sidekick's saved-account chooser; provider login and "
                "logout cannot bypass coordinated selection.",
            )
        return ClaudeCommandRoute(ClaudeCommandKind.PROVIDER)

    def select(self, account_id: SidekickAccountId) -> str:
        """Select one stable saved account through the shared transaction."""
        result = self._use.select(ProviderId.CLAUDE, account_id)
        if isinstance(result, UseSelectionFailure):
            return f"Sidekick could not select this account: {result.code}."
        return "Account selection is ready; the next request uses it."

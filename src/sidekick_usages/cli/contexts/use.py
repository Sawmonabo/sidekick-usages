"""Secret-free composition for scriptable account selection."""

from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.cli.dashboard.models.use import (
    UseSelectionFailure,
    UseSelectionResult,
    UseSelectionSuccess,
)
from sidekick_usages.cli.dashboard.ports import AccountSelection
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.selection.models import SelectionResult
from sidekick_usages.core.selection.types import (
    SelectionCode,
    SelectionOutcome,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import (
    ControlClient,
    consume_selection_action,
)
from sidekick_usages.daemon.models.protocol import (
    FailedPayload,
    IncompatiblePayload,
)
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.persistence.accounts.index import (
    AccountIndex,
)
from sidekick_usages.persistence.accounts.reader import AccountIndexReader


@dataclass(frozen=True, slots=True)
class UseContext:
    """Secret-free account lookup and supervisor selection boundary."""

    accounts: AccountIndex
    select: AccountSelection


@dataclass(frozen=True, slots=True)
class _SupervisorAccountSelection:
    """Send one sanitized selection request to the local supervisor."""

    _socket_path: Path

    def __call__(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
    ) -> UseSelectionResult:
        client = ControlClient.connect(self._socket_path)
        try:
            terminal = consume_selection_action(
                client.select_account(provider_id, account_id),
                provider_id=provider_id,
                account_id=account_id,
            )
            if isinstance(terminal, SelectionResult):
                if terminal.outcome is SelectionOutcome.READY:
                    return UseSelectionSuccess(terminal.ready_count)
                return UseSelectionFailure(terminal.safe_code.value)
            if isinstance(terminal, FailedPayload):
                if terminal.code == SelectionCode.ALREADY_SELECTED.value:
                    return UseSelectionSuccess()
                return UseSelectionFailure(terminal.code)
            if isinstance(terminal, IncompatiblePayload):
                return UseSelectionFailure("service_incompatible")
            return UseSelectionFailure("service_stopping")
        finally:
            client.close()


def compose_use_context(
    *,
    paths: ApplicationPaths | None = None,
    selection: AccountSelection | None = None,
) -> UseContext:
    """Compose only secret-free account lookup and local control access."""
    resolved_paths = discover_application_paths() if paths is None else paths
    resolved_selection = (
        _SupervisorAccountSelection(resolved_paths.supervisor_socket)
        if selection is None
        else selection
    )
    return UseContext(
        AccountIndex(AccountIndexReader(resolved_paths.accounts).load()),
        resolved_selection,
    )

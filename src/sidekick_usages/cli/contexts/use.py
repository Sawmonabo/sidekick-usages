"""Secret-free composition for scriptable account activation."""

from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.cli.dashboard.models.use import (
    UseActivationFailure,
    UseActivationResult,
    UseActivationSuccess,
)
from sidekick_usages.cli.dashboard.ports import AccountActivation
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import (
    ControlClient,
    consume_control_action,
)
from sidekick_usages.daemon.models.protocol import (
    CompletedPayload,
    FailedPayload,
    IncompatiblePayload,
)
from sidekick_usages.daemon.types.protocol import (
    CompletionOutcome,
    ControlOperationIdentity,
)
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.persistence.accounts.index import (
    AccountIndex,
)
from sidekick_usages.persistence.accounts.reader import AccountIndexReader


@dataclass(frozen=True, slots=True)
class UseContext:
    """Secret-free account lookup and supervisor activation boundary."""

    accounts: AccountIndex
    activate: AccountActivation


@dataclass(frozen=True, slots=True)
class _SupervisorAccountActivation:
    """Send one sanitized activation request to the local supervisor."""

    _socket_path: Path

    def __call__(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
    ) -> UseActivationResult:
        client = ControlClient.connect(self._socket_path)
        try:
            terminal = consume_control_action(
                client.activate(
                    provider_id,
                    account_id,
                ),
                identity=ControlOperationIdentity.ACCOUNT,
            )
            if isinstance(terminal, CompletedPayload):
                if terminal.outcome is CompletionOutcome.CANCELLED:
                    return UseActivationFailure("activation_cancelled")
                return UseActivationSuccess()
            if isinstance(terminal, FailedPayload):
                return UseActivationFailure(terminal.code)
            if isinstance(terminal, IncompatiblePayload):
                return UseActivationFailure("service_incompatible")
            return UseActivationFailure("service_stopping")
        finally:
            client.close()


def compose_use_context(
    *,
    paths: ApplicationPaths | None = None,
    activation: AccountActivation | None = None,
) -> UseContext:
    """Compose only secret-free account lookup and local control access."""
    resolved_paths = discover_application_paths() if paths is None else paths
    resolved_activation = (
        _SupervisorAccountActivation(resolved_paths.supervisor_socket)
        if activation is None
        else activation
    )
    return UseContext(
        AccountIndex(AccountIndexReader(resolved_paths.accounts).load()),
        resolved_activation,
    )

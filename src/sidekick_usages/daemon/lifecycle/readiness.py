"""Supervisor readiness and transient-state cleanup."""

import time
from collections.abc import Callable
from datetime import datetime
from typing import assert_never

from sidekick_usages import __version__
from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.identifiers import new_operation_id
from sidekick_usages.core.accounts.models import (
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.client import ControlClient
from sidekick_usages.daemon.control import cleanup_control_endpoint
from sidekick_usages.daemon.diagnostics import SanitizedDiagnosticLog
from sidekick_usages.daemon.lifecycle.errors import ServiceLifecycleError
from sidekick_usages.daemon.models.lifecycle import (
    ServiceBackendStatus,
    SupervisorHealth,
)
from sidekick_usages.daemon.models.service import ServiceState
from sidekick_usages.daemon.protocol import PROTOCOL_VERSION
from sidekick_usages.daemon.types.lifecycle import (
    ServiceComponentState,
    ServiceFailureCode,
    ServiceLifecycleState,
)
from sidekick_usages.daemon.types.protocol import EventKind
from sidekick_usages.daemon.types.service import (
    PackageVersion,
    ServicePhase,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.activation_journal import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.operation_queue import OperationQueueStore
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.service_state import ServiceStateStore

__all__ = ["RuntimeCleanup", "SupervisorReadiness"]

_READINESS_TIMEOUT_SECONDS = 30.0
_READINESS_WAIT_SECONDS = 0.1


class SupervisorReadiness:
    """Enroll saved accounts and prove one bounded resident-service pass."""

    def __init__(
        self,
        paths: ApplicationPaths,
        clock: Clock,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._paths = paths
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._queue = OperationQueueStore(paths.durable_operations)
        self._state = ServiceStateStore(paths.service_state)

    def enroll_accounts(self) -> None:
        """Persist one immediately due maintenance slot per saved account."""
        now = self._clock.now()
        try:
            for account in self._accounts():
                self._queue.enqueue(
                    DueOperation(
                        operation_id=new_operation_id(),
                        provider_id=account.provider_id,
                        account_id=account.account_id,
                        kind=OperationKind.MAINTAIN,
                        priority=OperationPriority.SCHEDULED,
                        state=OperationState.SCHEDULED,
                        due_at=now,
                        updated_at=now,
                    )
                )
        except PersistenceError, ValueError:
            raise ServiceLifecycleError(
                ServiceFailureCode.QUEUE_INCOMPLETE
            ) from None

    def verify_ready(self) -> None:
        """Verify handshake, current service state, queue, and Codex phase."""
        self._verify_handshake()
        try:
            state = self._state.load()
            accounts = self._accounts()
            operations = self._queue.load()
        except PersistenceError, ValueError:
            raise ServiceLifecycleError(
                ServiceFailureCode.SERVICE_UNHEALTHY
            ) from None
        if (
            state is None
            or state.protocol_version != PROTOCOL_VERSION
            or state.package_version != PackageVersion(__version__)
            or state.phase is not ServicePhase.READY
        ):
            raise ServiceLifecycleError(ServiceFailureCode.SERVICE_UNHEALTHY)
        enrolled = {
            operation.account_id
            for operation in operations
            if operation.kind is OperationKind.MAINTAIN
        }
        if any(account.account_id not in enrolled for account in accounts):
            raise ServiceLifecycleError(ServiceFailureCode.QUEUE_INCOMPLETE)
        if any(_requires_codex_broker(account) for account in accounts):
            raise ServiceLifecycleError(
                ServiceFailureCode.CODEX_BROKER_UNAVAILABLE
            )

    def complete_maintenance_pass(self) -> None:
        """Wake maintenance and wait for each enrolled slot to settle."""
        self._request_maintenance()
        deadline = self._monotonic() + _READINESS_TIMEOUT_SECONDS
        while True:
            now = self._clock.now()
            try:
                accounts = self._accounts()
                operations = {
                    operation.account_id: operation
                    for operation in self._queue.load()
                    if operation.kind is OperationKind.MAINTAIN
                }
            except PersistenceError, ValueError:
                raise ServiceLifecycleError(
                    ServiceFailureCode.QUEUE_INCOMPLETE
                ) from None
            if all(
                _maintenance_settled(operations.get(account.account_id), now)
                for account in accounts
            ):
                return
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise ServiceLifecycleError(
                    ServiceFailureCode.MAINTENANCE_TIMEOUT
                )
            self._sleep(min(_READINESS_WAIT_SECONDS, remaining))

    def health(self, status: ServiceBackendStatus) -> SupervisorHealth:
        """Inspect components independently without installing or repairing."""
        platform, process = _backend_health(status.state)
        accounts_readable = True
        try:
            accounts = self._accounts()
        except PersistenceError, ValueError:
            accounts = ()
            accounts_readable = False
        broker = _broker_health(accounts, accounts_readable)
        unavailable = (
            ServiceComponentState.FEATURE_DISABLED
            if process is ServiceComponentState.FEATURE_DISABLED
            else ServiceComponentState.UNAVAILABLE
        )
        if (
            unavailable is ServiceComponentState.FEATURE_DISABLED
            and broker is not ServiceComponentState.NOT_REQUIRED
        ):
            broker = ServiceComponentState.FEATURE_DISABLED
        if process is not ServiceComponentState.HEALTHY:
            return SupervisorHealth(
                backend=status.backend,
                cli_version=PackageVersion(__version__),
                supervisor_version=None,
                platform=platform,
                process=process,
                protocol=unavailable,
                queue=unavailable,
                journal=unavailable,
                broker=broker,
            )

        state_readable = True
        try:
            state = self._state.load()
        except PersistenceError, ValueError:
            state = None
            state_readable = False
        return SupervisorHealth(
            backend=status.backend,
            cli_version=PackageVersion(__version__),
            supervisor_version=(
                None if state is None else state.package_version
            ),
            platform=platform,
            process=process,
            protocol=self._protocol_health(state, state_readable),
            queue=self._queue_health(
                state,
                state_readable,
                accounts,
                accounts_readable,
            ),
            journal=self._journal_health(state, state_readable),
            broker=broker,
        )

    def _protocol_health(
        self,
        state: ServiceState | None,
        state_readable: bool,
    ) -> ServiceComponentState:
        """Inspect socket negotiation and persisted version agreement."""
        try:
            self._verify_handshake()
        except ServiceLifecycleError:
            return ServiceComponentState.UNHEALTHY
        if not state_readable:
            return ServiceComponentState.UNHEALTHY
        if state is None:
            return ServiceComponentState.UNAVAILABLE
        if (
            state.protocol_version != PROTOCOL_VERSION
            or state.package_version != PackageVersion(__version__)
        ):
            return ServiceComponentState.UNHEALTHY
        return ServiceComponentState.HEALTHY

    def _queue_health(
        self,
        state: ServiceState | None,
        state_readable: bool,
        accounts: tuple[SavedAccount, ...],
        accounts_readable: bool,
    ) -> ServiceComponentState:
        """Inspect durable scheduler recovery and account enrollment."""
        if not state_readable or not accounts_readable:
            return ServiceComponentState.UNHEALTHY
        if state is None:
            return ServiceComponentState.UNAVAILABLE
        if not state.queue_recovered:
            return ServiceComponentState.UNHEALTHY
        try:
            operations = self._queue.load()
        except PersistenceError, ValueError:
            return ServiceComponentState.UNHEALTHY
        enrolled = {
            operation.account_id
            for operation in operations
            if operation.kind is OperationKind.MAINTAIN
        }
        if any(account.account_id not in enrolled for account in accounts):
            return ServiceComponentState.UNHEALTHY
        return ServiceComponentState.HEALTHY

    def _journal_health(
        self,
        state: ServiceState | None,
        state_readable: bool,
    ) -> ServiceComponentState:
        """Inspect persisted recovery proof and unfinished activations."""
        if not state_readable:
            return ServiceComponentState.UNHEALTHY
        if state is None:
            return ServiceComponentState.UNAVAILABLE
        if not state.journals_reconciled:
            return ServiceComponentState.UNHEALTHY
        journals = ActivationJournalStore(self._paths.activation_journals)
        try:
            unfinished = any(
                journals.load(provider_id).active is not None
                for provider_id in ProviderId
            )
        except PersistenceError, ValueError:
            return ServiceComponentState.UNHEALTHY
        return (
            ServiceComponentState.UNHEALTHY
            if unfinished
            else ServiceComponentState.HEALTHY
        )

    def _accounts(self) -> tuple[SavedAccount, ...]:
        private = PrivateCredentialTree(
            self._paths.private_credentials,
            account_path=self._paths.accounts,
        )
        return (
            AccountStore(self._paths.accounts, private).load().saved_accounts()
        )

    def _verify_handshake(self) -> None:
        try:
            client = ControlClient.connect(self._paths.supervisor_socket)
            try:
                client.handshake()
            finally:
                client.close()
        except OSError, ValueError:
            raise ServiceLifecycleError(
                ServiceFailureCode.HANDSHAKE_FAILED
            ) from None

    def _request_maintenance(self) -> None:
        try:
            client = ControlClient.connect(self._paths.supervisor_socket)
            try:
                events = tuple(client.refresh_all())
            finally:
                client.close()
        except OSError, ValueError:
            raise ServiceLifecycleError(
                ServiceFailureCode.HANDSHAKE_FAILED
            ) from None
        if not events or events[-1].kind is not EventKind.COMPLETED:
            raise ServiceLifecycleError(ServiceFailureCode.SERVICE_UNHEALTHY)


class RuntimeCleanup:
    """Remove only supervisor-owned transient state."""

    def __init__(self, paths: ApplicationPaths) -> None:
        self._paths = paths

    def clear(self) -> None:
        """Remove the socket, service state, and sanitized service logs."""
        try:
            cleanup_control_endpoint(
                self._paths.runtime_directory,
                self._paths.supervisor_socket,
            )
            ServiceStateStore(self._paths.service_state).clear()
            SanitizedDiagnosticLog(self._paths.service_logs).clear()
        except OSError, PersistenceError:
            raise ServiceLifecycleError(
                ServiceFailureCode.ARTIFACT_UNSAFE
            ) from None


def _requires_codex_broker(account: SavedAccount) -> bool:
    authority = account.authority
    return isinstance(
        authority,
        CodexAccountAuthority,
    ) and isinstance(authority.subscription, CodexManagedAuthority)


def _backend_health(
    state: ServiceLifecycleState,
) -> tuple[ServiceComponentState, ServiceComponentState]:
    match state:
        case ServiceLifecycleState.ABSENT:
            return (
                ServiceComponentState.HEALTHY,
                ServiceComponentState.ABSENT,
            )
        case ServiceLifecycleState.READY:
            return (
                ServiceComponentState.HEALTHY,
                ServiceComponentState.HEALTHY,
            )
        case ServiceLifecycleState.INSTALLED:
            return (
                ServiceComponentState.HEALTHY,
                ServiceComponentState.UNHEALTHY,
            )
        case ServiceLifecycleState.UNHEALTHY:
            return (
                ServiceComponentState.UNHEALTHY,
                ServiceComponentState.UNHEALTHY,
            )
        case ServiceLifecycleState.FEATURE_DISABLED:
            return (
                ServiceComponentState.FEATURE_DISABLED,
                ServiceComponentState.FEATURE_DISABLED,
            )
    return assert_never(state)


def _broker_health(
    accounts: tuple[SavedAccount, ...],
    accounts_readable: bool,
) -> ServiceComponentState:
    if not accounts_readable:
        return ServiceComponentState.UNAVAILABLE
    if any(_requires_codex_broker(account) for account in accounts):
        return ServiceComponentState.UNAVAILABLE
    return ServiceComponentState.NOT_REQUIRED


def _maintenance_settled(
    operation: DueOperation | None,
    now: datetime,
) -> bool:
    if operation is None:
        return False
    if operation.state is OperationState.ACTION_REQUIRED:
        return True
    return (
        operation.state
        in {OperationState.SCHEDULED, OperationState.RETRY_WAIT}
        and operation.due_at > now
    )

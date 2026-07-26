"""Supervisor readiness and transient-state cleanup."""

import time
from collections.abc import Callable
from datetime import datetime
from threading import Event, Lock
from typing import assert_never

from sidekick_usages import __version__
from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.identifiers import new_operation_id
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import (
    ControlClient,
    ServiceCompatibilityError,
    UnexpectedServiceEventError,
)
from sidekick_usages.daemon.control.endpoint import control_endpoint_state
from sidekick_usages.daemon.control.server import cleanup_control_endpoint
from sidekick_usages.daemon.lifecycle.errors import ServiceLifecycleError
from sidekick_usages.daemon.lifecycle.ports import (
    ProviderCapabilityReadiness,
    ServiceLifecycleObserver,
    discard_service_lifecycle_observation,
)
from sidekick_usages.daemon.models.lifecycle import (
    ServiceBackendStatus,
    ServiceLifecycleObservation,
    SupervisorHealth,
)
from sidekick_usages.daemon.models.service import (
    ServiceState,
    requires_codex_broker,
)
from sidekick_usages.daemon.runtime.diagnostics import SanitizedDiagnosticLog
from sidekick_usages.daemon.types.lifecycle import (
    ProviderReadinessScope,
    ServiceComponentState,
    ServiceFailureCode,
    ServiceLifecyclePhase,
    ServiceLifecycleState,
)
from sidekick_usages.daemon.types.protocol import PROTOCOL_VERSION, EventKind
from sidekick_usages.daemon.types.service import (
    PackageVersion,
)
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.reader import AccountIndexReader
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.queue import OperationQueueStore
from sidekick_usages.persistence.supervisor.service import ServiceStateStore
from sidekick_usages.platform.peer import PeerVerificationError

_READINESS_TIMEOUT_SECONDS = 30.0
_READINESS_WAIT_SECONDS = 0.1
_TRANSIENT_READINESS_FAILURES = frozenset(
    {
        ServiceFailureCode.CODEX_BROKER_UNAVAILABLE,
        ServiceFailureCode.SERVICE_UNHEALTHY,
    }
)


class SupervisorReadiness:
    """Enroll saved accounts and prove one bounded resident-service pass."""

    def __init__(
        self,
        paths: ApplicationPaths,
        clock: Clock,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        provider_readiness: ProviderCapabilityReadiness | None = None,
    ) -> None:
        self._paths = paths
        self._clock = clock
        self._monotonic = monotonic
        self._queue = OperationQueueStore(paths.durable_operations)
        self._state = ServiceStateStore(paths.service_state)
        self._cancelled = Event()
        self._client_lock = Lock()
        self._active_client: ControlClient | None = None
        self._provider_readiness = provider_readiness

    def cancel(self) -> None:
        """Interrupt active local-control observation."""
        self._cancelled.set()
        provider_readiness = self._provider_readiness
        if provider_readiness is not None:
            provider_readiness.cancel()
        with self._client_lock:
            client = self._active_client
        if client is not None:
            client.close()

    def enroll_accounts(self) -> None:
        """Persist one immediately due maintenance slot per saved account."""
        self._raise_if_cancelled()
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

    def verify_ready(
        self,
        provider_ids: ProviderReadinessScope = (),
        *,
        progress: ServiceLifecycleObserver = (
            discard_service_lifecycle_observation
        ),
    ) -> None:
        """Verify service state plus each requested provider capability."""
        self._raise_if_cancelled()
        self._verify_handshake(progress)
        progress(
            ServiceLifecycleObservation(ServiceLifecyclePhase.DURABLE_RECOVERY)
        )
        state, accounts, operations = self._load_readiness_state()
        broker_required = _broker_required(accounts, provider_ids)
        if broker_required:
            progress(
                ServiceLifecycleObservation(
                    ServiceLifecyclePhase.CODEX_BROKER
                )
            )
        self._require_resident_readiness(
            state,
            accounts,
            operations,
            broker_required=broker_required,
        )
        self._require_provider_readiness(provider_ids, progress)

    def wait_until_ready(
        self,
        provider_ids: ProviderReadinessScope = (),
        *,
        progress: ServiceLifecycleObserver = (
            discard_service_lifecycle_observation
        ),
    ) -> None:
        """Wait for bounded resident startup, then verify each provider."""
        self._raise_if_cancelled()
        self._verify_handshake(progress)
        progress(
            ServiceLifecycleObservation(ServiceLifecyclePhase.DURABLE_RECOVERY)
        )
        deadline = self._monotonic() + _READINESS_TIMEOUT_SECONDS
        broker_reported = False
        while True:
            self._raise_if_cancelled()
            try:
                state, accounts, operations = self._load_readiness_state()
                broker_required = _broker_required(accounts, provider_ids)
                if broker_required and not broker_reported:
                    progress(
                        ServiceLifecycleObservation(
                            ServiceLifecyclePhase.CODEX_BROKER
                        )
                    )
                    broker_reported = True
                self._require_resident_readiness(
                    state,
                    accounts,
                    operations,
                    broker_required=broker_required,
                )
            except ServiceLifecycleError as error:
                if error.code not in _TRANSIENT_READINESS_FAILURES:
                    raise
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise
                self._cancelled.wait(
                    min(_READINESS_WAIT_SECONDS, remaining)
                )
                continue
            break
        self._require_provider_readiness(provider_ids, progress)

    def _load_readiness_state(
        self,
    ) -> tuple[
        ServiceState, tuple[SavedAccount, ...], tuple[DueOperation, ...]
    ]:
        """Load one internally compatible resident recovery snapshot."""
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
        ):
            raise ServiceLifecycleError(ServiceFailureCode.SERVICE_UNHEALTHY)
        return state, accounts, operations

    @staticmethod
    def _require_resident_readiness(
        state: ServiceState,
        accounts: tuple[SavedAccount, ...],
        operations: tuple[DueOperation, ...],
        *,
        broker_required: bool,
    ) -> None:
        """Prove durable recovery and the requested Codex broker."""
        if not state.queue_recovered or not state.journals_reconciled:
            raise ServiceLifecycleError(ServiceFailureCode.SERVICE_UNHEALTHY)
        enrolled = {
            operation.account_id
            for operation in operations
            if operation.kind is OperationKind.MAINTAIN
        }
        if any(account.account_id not in enrolled for account in accounts):
            raise ServiceLifecycleError(ServiceFailureCode.QUEUE_INCOMPLETE)
        if broker_required and not state.broker_ready:
            raise ServiceLifecycleError(
                ServiceFailureCode.CODEX_BROKER_UNAVAILABLE
            )
        if not state.ready_for(broker_required=broker_required):
            raise ServiceLifecycleError(ServiceFailureCode.SERVICE_UNHEALTHY)

    def _require_provider_readiness(
        self,
        provider_ids: ProviderReadinessScope,
        progress: ServiceLifecycleObserver,
    ) -> None:
        """Prove each requested provider at its authoritative adapter."""
        provider_readiness = self._provider_readiness
        for provider_id in provider_ids:
            self._raise_if_cancelled()
            progress(
                ServiceLifecycleObservation(
                    ServiceLifecyclePhase.PROVIDER_CAPABILITY,
                    provider_id,
                )
            )
            if provider_readiness is None or not provider_readiness.ready(
                provider_id
            ):
                self._raise_if_cancelled()
                raise ServiceLifecycleError(
                    ServiceFailureCode.PROVIDER_CAPABILITY_UNAVAILABLE,
                    provider_id=provider_id,
                )

    def complete_maintenance_pass(
        self,
        progress: ServiceLifecycleObserver = (
            discard_service_lifecycle_observation
        ),
    ) -> None:
        """Wake maintenance and wait for each enrolled slot to settle."""
        progress(
            ServiceLifecycleObservation(
                ServiceLifecyclePhase.MAINTENANCE_COMPLETED
            )
        )
        self._request_maintenance()
        deadline = self._monotonic() + _READINESS_TIMEOUT_SECONDS
        while True:
            self._raise_if_cancelled()
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
            self._cancelled.wait(min(_READINESS_WAIT_SECONDS, remaining))

    def health(self, status: ServiceBackendStatus) -> SupervisorHealth:
        """Inspect components independently without installing or repairing."""
        platform = _platform_health(status.state)
        process = status.process
        socket = control_endpoint_state(
            self._paths.runtime_directory,
            self._paths.supervisor_socket,
        )
        accounts_readable = True
        try:
            accounts = self._observe_accounts()
        except PersistenceError, ValueError:
            accounts = ()
            accounts_readable = False
        broker = _broker_health(
            accounts,
            accounts_readable,
            None,
            False,
        )
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
                rescue=status.rescue,
                socket=(
                    ServiceComponentState.FEATURE_DISABLED
                    if process is ServiceComponentState.FEATURE_DISABLED
                    else socket
                ),
                peer=unavailable,
                protocol=unavailable,
                queue=unavailable,
                journal=unavailable,
                broker=broker,
                broker_failure_code=None,
            )

        state_readable = True
        try:
            state = self._state.observe()
        except PersistenceError, ValueError:
            state = None
            state_readable = False
        broker = _broker_health(
            accounts,
            accounts_readable,
            state,
            state_readable,
        )
        peer, handshake = self._control_health(socket)
        return SupervisorHealth(
            backend=status.backend,
            cli_version=PackageVersion(__version__),
            supervisor_version=(
                None if state is None else state.package_version
            ),
            platform=platform,
            process=process,
            rescue=status.rescue,
            socket=socket,
            peer=peer,
            protocol=self._protocol_health(
                handshake,
                state,
                state_readable,
            ),
            queue=self._queue_health(
                state,
                state_readable,
                accounts,
                accounts_readable,
            ),
            journal=self._journal_health(state, state_readable),
            broker=broker,
            broker_failure_code=_broker_failure_code(
                broker,
                state,
                state_readable,
            ),
        )

    def _protocol_health(
        self,
        handshake: ServiceComponentState,
        state: ServiceState | None,
        state_readable: bool,
    ) -> ServiceComponentState:
        """Inspect socket negotiation and persisted version agreement."""
        if handshake is not ServiceComponentState.HEALTHY:
            return handshake
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

    def _control_health(
        self,
        socket: ServiceComponentState,
    ) -> tuple[ServiceComponentState, ServiceComponentState]:
        """Observe peer proof and protocol handshake as separate phases."""
        if socket is not ServiceComponentState.HEALTHY:
            return (
                ServiceComponentState.UNAVAILABLE,
                ServiceComponentState.UNAVAILABLE,
            )
        try:
            client = self._connect_client()
        except PeerVerificationError:
            return (
                ServiceComponentState.UNHEALTHY,
                ServiceComponentState.UNAVAILABLE,
            )
        except OSError, ValueError:
            return (
                ServiceComponentState.UNAVAILABLE,
                ServiceComponentState.UNAVAILABLE,
            )
        try:
            client.handshake()
        except OSError, ValueError:
            return (
                ServiceComponentState.HEALTHY,
                ServiceComponentState.UNHEALTHY,
            )
        finally:
            self._release_client(client)
        return (
            ServiceComponentState.HEALTHY,
            ServiceComponentState.HEALTHY,
        )

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
            operations = self._queue.observe()
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
        journals = ActivationJournalStore(
            self._paths.activation_journals,
            self._paths.durable_operations,
        )
        try:
            unfinished = bool(journals.observe_all())
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

    def _observe_accounts(self) -> tuple[SavedAccount, ...]:
        return AccountIndexReader(self._paths.accounts).load()

    def _verify_handshake(
        self,
        progress: ServiceLifecycleObserver = (
            discard_service_lifecycle_observation
        ),
    ) -> None:
        progress(
            ServiceLifecycleObservation(ServiceLifecyclePhase.CONTROL_SOCKET)
        )
        deadline = self._monotonic() + _READINESS_TIMEOUT_SECONDS
        while True:
            try:
                client = self._connect_client()
                try:
                    client.handshake()
                finally:
                    self._release_client(client)
            except (
                PermissionError,
                ServiceCompatibilityError,
                UnexpectedServiceEventError,
                ValueError,
            ):
                self._raise_if_cancelled()
                raise ServiceLifecycleError(
                    ServiceFailureCode.HANDSHAKE_FAILED
                ) from None
            except (
                FileNotFoundError,
                TimeoutError,
                ConnectionError,
            ):
                self._raise_if_cancelled()
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise ServiceLifecycleError(
                        ServiceFailureCode.HANDSHAKE_FAILED
                    ) from None
                self._cancelled.wait(min(_READINESS_WAIT_SECONDS, remaining))
                continue
            except OSError:
                self._raise_if_cancelled()
                raise ServiceLifecycleError(
                    ServiceFailureCode.HANDSHAKE_FAILED
                ) from None
            return

    def _request_maintenance(self) -> None:
        try:
            client = self._connect_client()
            try:
                events = tuple(client.refresh_all())
            finally:
                self._release_client(client)
        except OSError, ValueError:
            self._raise_if_cancelled()
            raise ServiceLifecycleError(
                ServiceFailureCode.HANDSHAKE_FAILED
            ) from None
        if not events or events[-1].kind is not EventKind.COMPLETED:
            raise ServiceLifecycleError(ServiceFailureCode.SERVICE_UNHEALTHY)

    def _connect_client(self) -> ControlClient:
        self._raise_if_cancelled()
        client = ControlClient.connect(self._paths.supervisor_socket)
        with self._client_lock:
            if self._cancelled.is_set():
                client.close()
                raise ServiceLifecycleError(ServiceFailureCode.CANCELLED)
            self._active_client = client
        return client

    def _release_client(self, client: ControlClient) -> None:
        client.close()
        with self._client_lock:
            if self._active_client is client:
                self._active_client = None

    def _raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise ServiceLifecycleError(ServiceFailureCode.CANCELLED)


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


def _platform_health(
    state: ServiceLifecycleState,
) -> ServiceComponentState:
    match state:
        case ServiceLifecycleState.ABSENT:
            return ServiceComponentState.HEALTHY
        case ServiceLifecycleState.READY | ServiceLifecycleState.INSTALLED:
            return ServiceComponentState.HEALTHY
        case ServiceLifecycleState.UNHEALTHY:
            return ServiceComponentState.UNHEALTHY
        case ServiceLifecycleState.FEATURE_DISABLED:
            return ServiceComponentState.FEATURE_DISABLED
    return assert_never(state)


def _broker_required(
    accounts: tuple[SavedAccount, ...],
    provider_ids: ProviderReadinessScope,
) -> bool:
    return any(requires_codex_broker(account) for account in accounts) and (
        not provider_ids or ProviderId.CODEX in provider_ids
    )


def _broker_health(
    accounts: tuple[SavedAccount, ...],
    accounts_readable: bool,
    state: ServiceState | None,
    state_readable: bool,
) -> ServiceComponentState:
    if not accounts_readable:
        return ServiceComponentState.UNAVAILABLE
    if not any(requires_codex_broker(account) for account in accounts):
        return ServiceComponentState.NOT_REQUIRED
    if not state_readable:
        return ServiceComponentState.UNHEALTHY
    if state is None:
        return ServiceComponentState.UNAVAILABLE
    return (
        ServiceComponentState.HEALTHY
        if state.broker_ready
        else ServiceComponentState.UNHEALTHY
    )


def _broker_failure_code(
    broker: ServiceComponentState,
    state: ServiceState | None,
    state_readable: bool,
) -> str | None:
    if (
        broker is not ServiceComponentState.UNHEALTHY
        or not state_readable
        or state is None
        or not state.broker_degraded()
    ):
        return None
    return state.failure_code


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

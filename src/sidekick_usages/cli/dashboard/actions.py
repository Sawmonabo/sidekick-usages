"""Serialized local-supervisor actions for the interactive dashboard."""

from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import assert_never

from sidekick_usages.cli.dashboard.models.controller import (
    ActivateOrRepairIntent,
    DashboardIntent,
    RefreshAccountIntent,
    RefreshDueAccountsIntent,
)
from sidekick_usages.cli.dashboard.models.session import (
    DashboardActionRequest,
    DashboardConfirmationKind,
    DashboardStartupReconciliation,
    DashboardStartupReconciliationState,
)
from sidekick_usages.cli.dashboard.models.setup import (
    ServiceSetupDecision,
    ServiceSetupOutcome,
    ServiceSetupResult,
)
from sidekick_usages.cli.dashboard.ports import (
    DashboardActionSink,
    DashboardControlClient,
    DashboardControlConnector,
)
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import (
    UnexpectedServiceEventError,
    consume_control_action,
)
from sidekick_usages.daemon.control.protocol import ProtocolFailureError
from sidekick_usages.daemon.models.lifecycle import (
    ServiceLifecycleObservation,
)
from sidekick_usages.daemon.models.protocol import (
    CompletedPayload,
    ControlActionTerminalPayload,
    FailedPayload,
    SnapshotPayload,
)
from sidekick_usages.daemon.types.lifecycle import ServiceLifecyclePhase
from sidekick_usages.daemon.types.protocol import (
    CompletionOutcome,
    ControlOperationIdentity,
    EventKind,
    ProgressPhase,
)
from sidekick_usages.providers.claude.activation.types import (
    ClaudeActivationGuardFailure,
)

CONFIRMATION_RESPONSE_HINT = "y yes / n no"
REMOTE_CONTROL_CONFIRMATION_MESSAGE = (
    "Claude Remote Control may disconnect during this switch. "
    "Continue? y yes / n no"
)
REMOTE_CONTROL_REFUSED_MESSAGE = "Claude Remote Control approval was refused."
REMOTE_CONTROL_FAILURE_CODE = (
    ClaudeActivationGuardFailure.REMOTE_CONTROL_DISCONNECT_REQUIRED
).failure_code
CONTROL_PROGRESS_MESSAGES = {
    ProgressPhase.QUEUED: "Account action queued.",
    ProgressPhase.STARTING: "Starting account action.",
    ProgressPhase.RUNNING: "Updating provider account state.",
    ProgressPhase.VERIFYING: "Verifying provider account state.",
    ProgressPhase.RECONCILING: "Reconciling provider account state.",
}
STARTUP_RECONCILIATION_ATTEMPTS = 2


class DashboardActionExecutor:
    """Run one capacity-one dashboard action on its fixed owner thread."""

    def __init__(
        self,
        *,
        connector: DashboardControlConnector,
        socket_path: Path,
        setup: GuidedServiceSetup,
        sink: DashboardActionSink,
    ) -> None:
        self._connector = connector
        self._socket_path = socket_path
        self._setup = setup
        self._sink = sink
        self._client_lock = Lock()
        self._active_client: DashboardControlClient | None = None

    def execute(self, request: DashboardActionRequest) -> None:
        """Prepare and dispatch one exact request at most once per approval."""
        client = self._prepare_service(request.intent)
        if client is None:
            return
        terminal = self._dispatch_ready(client, request)
        if terminal is None:
            return
        if self._remote_control_confirmation_required(request, terminal):
            decision = self._sink.request_confirmation(
                DashboardConfirmationKind.REMOTE_CONTROL,
                REMOTE_CONTROL_CONFIRMATION_MESSAGE,
            )
            if (
                decision is not ServiceSetupDecision.APPROVED
                or self._sink.stopping
            ):
                if not self._sink.stopping:
                    self._sink.action_error(
                        request.intent,
                        REMOTE_CONTROL_REFUSED_MESSAGE,
                    )
                return
            client = self._connect_after_readiness()
            if client is None:
                self._setup_failed(
                    ServiceSetupResult(
                        intent=request.intent,
                        outcome=ServiceSetupOutcome.FAILED,
                    )
                )
                return
            approved = replace(
                request,
                allow_remote_control_disconnect=True,
            )
            terminal = self._dispatch_ready(client, approved)
            if terminal is None:
                return
        self._sink.action_completed(request.intent, terminal)

    def reconcile_startup(
        self,
        provider_ids: tuple[ProviderId, ...],
    ) -> None:
        """Reconcile displayed providers without setup or input blocking."""
        pending = provider_ids
        for attempt in range(STARTUP_RECONCILIATION_ATTEMPTS):
            failed: list[ProviderId] = []
            for provider_id in pending:
                if self._sink.stopping:
                    return
                verified = self._reconcile_provider(provider_id)
                if not verified:
                    failed.append(provider_id)
                final_attempt = (
                    attempt + 1 == STARTUP_RECONCILIATION_ATTEMPTS
                )
                state = DashboardStartupReconciliationState.VERIFIED
                if not verified:
                    state = (
                        DashboardStartupReconciliationState.UNAVAILABLE
                        if final_attempt
                        else DashboardStartupReconciliationState.RETRYING
                    )
                self._sink.startup_reconciled(
                    DashboardStartupReconciliation(
                        provider_id=provider_id,
                        state=state,
                    )
                )
            if not failed:
                return
            pending = tuple(failed)

    def _reconcile_provider(self, provider_id: ProviderId) -> bool:
        """Return whether one provider supplied a successful read-back."""
        client: DashboardControlClient | None = None
        try:
            client = self._connector(self._socket_path)
            self._retain_client(client)
            terminal = consume_control_action(
                client.reconcile(provider_id),
                identity=ControlOperationIdentity.PROVIDER,
            )
            return (
                isinstance(terminal, CompletedPayload)
                and terminal.outcome
                in {
                    CompletionOutcome.SUCCEEDED,
                    CompletionOutcome.NO_CHANGE,
                }
            )
        except (
            UnexpectedServiceEventError,
            OSError,
            ProtocolFailureError,
        ):
            return False
        finally:
            if client is not None:
                self._release_client(client)

    def close(self) -> None:
        """Stop observing without cancelling durable supervisor work."""
        self._setup.close()
        with self._client_lock:
            client = self._active_client
            self._active_client = None
        if client is not None:
            client.close()

    def _prepare_service(
        self,
        intent: DashboardIntent,
    ) -> DashboardControlClient | None:
        result = self._setup.prepare(
            service=self._sink.service,
            intent=intent,
            interactive=True,
            decision=ServiceSetupDecision.NOT_REQUESTED,
            progress=self._setup_progress,
        )
        if result.outcome is ServiceSetupOutcome.CONFIRMATION_REQUIRED:
            decision = self._sink.request_confirmation(
                DashboardConfirmationKind.SERVICE_SETUP,
                f"{result.message} {CONFIRMATION_RESPONSE_HINT}",
            )
            if self._sink.stopping:
                return None
            result = self._setup.prepare(
                service=self._sink.service,
                intent=result.intent,
                interactive=True,
                decision=decision,
                progress=self._setup_progress,
            )
        if result.outcome is not ServiceSetupOutcome.RESUME:
            self._setup_failed(result)
            return None
        client = self._connect_after_readiness()
        if client is None:
            self._setup_failed(
                ServiceSetupResult(
                    intent=result.intent,
                    outcome=ServiceSetupOutcome.FAILED,
                )
            )
        return client

    def _connect_after_readiness(self) -> DashboardControlClient | None:
        """Recheck the endpoint after exact provider-scoped readiness."""
        client: DashboardControlClient | None = None
        try:
            client = self._connector(self._socket_path)
            self._retain_client(client)
            events = tuple(client.snapshot())
            if len(events) != 1:
                raise UnexpectedServiceEventError(
                    "The service returned an invalid snapshot stream."
                )
            event = events[0]
            if event.kind is not EventKind.SNAPSHOT or not isinstance(
                event.payload,
                SnapshotPayload,
            ):
                raise UnexpectedServiceEventError(
                    "The service returned an invalid snapshot."
                )
            return client
        except (
            UnexpectedServiceEventError,
            OSError,
            ProtocolFailureError,
        ):
            if client is not None:
                self._release_client(client)
            return None

    def _dispatch(
        self,
        client: DashboardControlClient,
        request: DashboardActionRequest,
    ) -> ControlActionTerminalPayload:
        intent = request.intent
        if isinstance(intent, ActivateOrRepairIntent):
            events = client.activate(
                intent.provider_id,
                intent.account_id,
                allow_remote_control_disconnect=(
                    request.allow_remote_control_disconnect
                ),
            )
        elif isinstance(intent, RefreshAccountIntent):
            events = client.refresh_account(
                intent.provider_id,
                intent.account_id,
            )
        else:
            events = client.refresh_all()
        return consume_control_action(
            events,
            identity=(
                ControlOperationIdentity.GLOBAL
                if isinstance(intent, RefreshDueAccountsIntent)
                else ControlOperationIdentity.ACCOUNT
            ),
            progress=self._publish_progress,
        )

    def _dispatch_ready(
        self,
        client: DashboardControlClient,
        request: DashboardActionRequest,
    ) -> ControlActionTerminalPayload | None:
        """Dispatch once and release the exact observation connection."""
        try:
            return self._dispatch(client, request)
        except (
            UnexpectedServiceEventError,
            OSError,
            ProtocolFailureError,
        ):
            self._sink.action_failed(request.intent)
            return None
        finally:
            self._release_client(client)

    def _publish_progress(self, phase: ProgressPhase) -> None:
        self._sink.publish_progress(CONTROL_PROGRESS_MESSAGES[phase])

    @staticmethod
    def _remote_control_confirmation_required(
        request: DashboardActionRequest,
        terminal: ControlActionTerminalPayload,
    ) -> bool:
        return (
            isinstance(request.intent, ActivateOrRepairIntent)
            and request.intent.provider_id is ProviderId.CLAUDE
            and not request.allow_remote_control_disconnect
            and isinstance(terminal, FailedPayload)
            and terminal.code == REMOTE_CONTROL_FAILURE_CODE
        )

    def _setup_progress(
        self,
        observation: ServiceLifecycleObservation,
    ) -> None:
        self._sink.publish_progress(
            _service_setup_progress_message(observation)
        )

    def _setup_failed(
        self,
        result: ServiceSetupResult[DashboardIntent],
    ) -> None:
        message = str(result.message)
        if result.corrective_action is not None:
            message = f"{message} {result.corrective_action}"
        self._sink.action_error(result.intent, message)

    def _retain_client(self, client: DashboardControlClient) -> None:
        with self._client_lock:
            if self._sink.stopping:
                client.close()
                raise ConnectionError("The dashboard session is closing.")
            self._active_client = client

    def _release_client(self, client: DashboardControlClient) -> None:
        client.close()
        with self._client_lock:
            if self._active_client is client:
                self._active_client = None


def _service_setup_progress_message(
    observation: ServiceLifecycleObservation,
) -> str:
    """Map one closed lifecycle observation to sanitized dashboard copy."""
    match observation.phase:
        case ServiceLifecyclePhase.INSTALLING:
            message = "Installing the Sidekick user service."
        case ServiceLifecyclePhase.STARTING:
            message = "Starting the Sidekick user service."
        case ServiceLifecyclePhase.CONTROL_SOCKET:
            message = "Verifying the Sidekick control socket."
        case ServiceLifecyclePhase.DURABLE_RECOVERY:
            message = "Verifying durable account-maintenance recovery."
        case ServiceLifecyclePhase.CODEX_BROKER:
            message = "Verifying the Codex account broker."
        case ServiceLifecyclePhase.PROVIDER_CAPABILITY:
            provider_id = observation.provider_id
            if provider_id is None:
                raise AssertionError(
                    "Provider capability progress lost its provider."
                )
            message = (
                f"Verifying {provider_id.value.title()} CLI capabilities."
            )
        case ServiceLifecyclePhase.MAINTENANCE_PASS:
            message = "Verifying the initial account-maintenance pass."
        case ServiceLifecyclePhase.RESTARTING:
            message = "Restarting the Sidekick user service."
        case _:
            assert_never(observation.phase)
    return message

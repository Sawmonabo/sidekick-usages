"""Serialized local-supervisor actions for the interactive dashboard."""

from dataclasses import replace
from pathlib import Path
from threading import Lock

from sidekick_usages.cli.dashboard.models.controller import (
    ActivateOrRepairIntent,
    DashboardIntent,
    RefreshAccountIntent,
    RefreshDueAccountsIntent,
)
from sidekick_usages.cli.dashboard.models.session import (
    DashboardActionRequest,
    DashboardConfirmationKind,
)
from sidekick_usages.cli.dashboard.models.setup import (
    ServiceSetupDecision,
    ServiceSetupOutcome,
    ServiceSetupProgress,
    ServiceSetupResult,
)
from sidekick_usages.cli.dashboard.ports import (
    DashboardActionSink,
    DashboardControlClient,
    DashboardControlConnector,
)
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.daemon.control.client import (
    UnexpectedServiceEventError,
    consume_control_action,
)
from sidekick_usages.daemon.control.protocol import ProtocolFailureError
from sidekick_usages.daemon.models.protocol import (
    ControlActionTerminalPayload,
    FailedPayload,
    SnapshotPayload,
)
from sidekick_usages.daemon.types.protocol import (
    ControlOperationIdentity,
    EventKind,
    ProgressPhase,
)
from sidekick_usages.providers.claude.activation.types import (
    ClaudeActivationGuardFailure,
)

SERVICE_RETRY_ACTION = (
    "Retry the action; run sidekick-usages daemon status if it fails again."
)
SERVICE_CONFIRMATION_MESSAGE = (
    "Sidekick needs its per-user service. Install it without administrator "
    "access? y yes / n no"
)
REMOTE_CONTROL_CONFIRMATION_MESSAGE = (
    "Claude Remote Control may disconnect during this switch. "
    "Continue? y yes / n no"
)
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
                self._sink.action_failed(request.intent)
                return
            client = self._connect_ready()
            if client is None:
                self._sink.action_failed(request.intent)
                return
            approved = replace(
                request,
                allow_remote_control_disconnect=True,
            )
            terminal = self._dispatch_ready(client, approved)
            if terminal is None:
                return
        self._sink.action_completed(request.intent, terminal)

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
        client = self._connect_ready()
        if client is not None:
            return client
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
                SERVICE_CONFIRMATION_MESSAGE,
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
        client = self._connect_ready()
        if client is None:
            self._sink.action_error(SERVICE_RETRY_ACTION)
        return client

    def _connect_ready(self) -> DashboardControlClient | None:
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
            if event.payload.ready:
                return client
        except (
            UnexpectedServiceEventError,
            OSError,
            ProtocolFailureError,
        ):
            pass
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
            and not request.allow_remote_control_disconnect
            and isinstance(terminal, FailedPayload)
            and terminal.code == REMOTE_CONTROL_FAILURE_CODE
        )

    def _setup_progress(self, progress: ServiceSetupProgress) -> None:
        self._sink.publish_progress(str(progress))

    def _setup_failed(
        self,
        result: ServiceSetupResult[DashboardIntent],
    ) -> None:
        message = str(result.message)
        if result.corrective_action is not None:
            message = f"{message} {result.corrective_action}"
        self._sink.action_error(message)

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

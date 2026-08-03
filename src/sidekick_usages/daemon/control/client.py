"""Same-user supervisor control client."""

import socket
import sys
from collections.abc import Callable, Generator, Iterator
from pathlib import Path

from sidekick_usages import __version__
from sidekick_usages.core.accounts.identifiers import new_request_id
from sidekick_usages.core.accounts.types import (
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionOutcome,
    SelectionPhase,
    TurnId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.endpoint import control_endpoint_state
from sidekick_usages.daemon.control.protocol import (
    FramedTransport,
)
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    AccountPayload,
    CompletedPayload,
    ControlActionTerminalPayload,
    ControlEvent,
    ControlRequest,
    EmptyPayload,
    FailedPayload,
    IncompatiblePayload,
    ProgressPayload,
    ProviderPayload,
    RequestPayload,
    ServiceStoppingPayload,
)
from sidekick_usages.daemon.selection.coordinator import (
    OLD_TURN_DRAIN_TIMEOUT_SECONDS,
    PARTICIPANT_READY_TIMEOUT_SECONDS,
)
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionProof,
    ParticipantAdoptionRequest,
    ParticipantConnectionRequest,
    ParticipantManifest,
    ParticipantReadyProof,
    ParticipantReadyRequest,
    SelectionStatus,
    TurnBeginRequest,
    TurnEndRequest,
)
from sidekick_usages.daemon.types.lifecycle import ServiceComponentState
from sidekick_usages.daemon.types.protocol import (
    PROTOCOL_VERSION,
    ConnectedSocket,
    ControlOperationIdentity,
    EventKind,
    ProgressPhase,
    ProtocolErrorCode,
    RequestKind,
)
from sidekick_usages.daemon.worker.pool import GENERAL_WORKER_TIMEOUT_SECONDS
from sidekick_usages.platform.peer import OperatingSystemPeerVerifier

_LOCAL_RESPONSE_TIMEOUT_SECONDS = 5.0
CONTROL_ACTION_TIMEOUT_SECONDS = (
    OLD_TURN_DRAIN_TIMEOUT_SECONDS
    + PARTICIPANT_READY_TIMEOUT_SECONDS
    + (2 * GENERAL_WORKER_TIMEOUT_SECONDS)
    + _LOCAL_RESPONSE_TIMEOUT_SECONDS
)
_LONG_ACTION_REQUEST_KINDS = frozenset(
    {
        RequestKind.RECONCILE,
        RequestKind.REFRESH_ACCOUNT,
    }
)
_EXTENDED_STREAM_REQUEST_KINDS = _LONG_ACTION_REQUEST_KINDS | {
    RequestKind.SUBSCRIBE,
    RequestKind.PARTICIPANT_SUBSCRIBE,
    RequestKind.SELECT_ACCOUNT,
}


class ServiceCompatibilityError(ConnectionError):
    """The installed client and resident service cannot safely cooperate."""

    def __init__(self, code: ProtocolErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class UnexpectedServiceEventError(ConnectionError):
    """The service broke request correlation or event ordering."""


def consume_control_action(
    events: Iterator[ControlEvent],
    *,
    identity: ControlOperationIdentity,
    progress: Callable[[ProgressPhase], None] | None = None,
) -> ControlActionTerminalPayload:
    """Validate one accepted, correlated control-action stream."""
    accepted = False
    operation_id: OperationId | None = None
    for event in events:
        if event.kind is EventKind.ACCEPTED:
            operation_id = _accepted_operation(event, accepted, identity)
            accepted = True
            continue
        if not accepted:
            payload = event.payload
            if (
                event.kind is EventKind.FAILED
                and isinstance(payload, FailedPayload)
                and payload.operation_id is None
            ):
                return payload
            raise UnexpectedServiceEventError(
                "The service returned progress before acceptance."
            )
        if event.kind is EventKind.PROGRESS:
            phase = _progress_phase(event, operation_id)
            if progress is not None:
                progress(phase)
            continue
        return _terminal_payload(event, operation_id)
    raise UnexpectedServiceEventError(
        "The service returned no terminal action event."
    )


def consume_selection_action(
    events: Iterator[ControlEvent],
    *,
    provider_id: ProviderId,
    account_id: SidekickAccountId,
    status: Callable[[SelectionStatus], None] | None = None,
) -> (
    SelectionResult
    | FailedPayload
    | IncompatiblePayload
    | ServiceStoppingPayload
):
    """Validate one accepted, correlated global-selection stream."""
    try:
        first = next(events)
    except StopIteration:
        raise UnexpectedServiceEventError(
            "The service returned no selection event."
        ) from None
    if first.kind is not EventKind.ACCEPTED:
        return _unaccepted_selection(first)
    operation_id = _selection_operation(first)
    previous_status: SelectionStatus | None = None
    for event in events:
        if event.kind is EventKind.SELECTION_STATUS:
            current_status = _selection_status(
                event,
                operation_id=operation_id,
                provider_id=provider_id,
                account_id=account_id,
                previous_status=previous_status,
            )
            previous_status = current_status
            if status is not None:
                status(current_status)
            continue
        return _selection_terminal(
            event,
            operation_id=operation_id,
            provider_id=provider_id,
            account_id=account_id,
            pending_epoch=(
                None
                if previous_status is None
                else previous_status.pending_epoch
            ),
        )
    raise UnexpectedServiceEventError(
        "The service returned no selection result."
    )


def _selection_status(
    event: ControlEvent,
    *,
    operation_id: OperationId,
    provider_id: ProviderId,
    account_id: SidekickAccountId,
    previous_status: SelectionStatus | None,
) -> SelectionStatus:
    """Return one correlated, causally ordered phase snapshot."""
    status = event.payload
    if (
        not isinstance(status, SelectionStatus)
        or status.operation_id != operation_id
        or status.provider_id is not provider_id
        or status.target_account_id != account_id
        or status.phase is None
    ):
        raise UnexpectedServiceEventError(
            "The service returned unrelated selection status."
        )
    previous_phase = None if previous_status is None else previous_status.phase
    allowed = {
        None: {
            SelectionPhase.PREVALIDATING,
            SelectionPhase.PREPARING,
            SelectionPhase.WAITING_OLD_TURNS,
            SelectionPhase.COMMITTING,
            SelectionPhase.AWAITING_READY,
            SelectionPhase.RECOVERING,
        },
        SelectionPhase.PREVALIDATING: {
            SelectionPhase.WAITING_OLD_TURNS,
            SelectionPhase.RECOVERING,
        },
        SelectionPhase.PREPARING: {
            SelectionPhase.WAITING_OLD_TURNS,
            SelectionPhase.RECOVERING,
        },
        SelectionPhase.WAITING_OLD_TURNS: {SelectionPhase.COMMITTING},
        SelectionPhase.COMMITTING: {
            SelectionPhase.AWAITING_READY,
            SelectionPhase.RECOVERING,
        },
        SelectionPhase.AWAITING_READY: {SelectionPhase.RECOVERING},
        SelectionPhase.RECOVERING: set(),
    }
    if status.phase not in allowed.get(previous_phase, set()):
        raise UnexpectedServiceEventError(
            "The service returned out-of-order selection status."
        )
    if previous_status is not None and (
        status.pending_epoch != previous_status.pending_epoch
        or status.finalized_epoch != previous_status.finalized_epoch
        or status.finalized_account_id != previous_status.finalized_account_id
    ):
        raise UnexpectedServiceEventError(
            "The service changed selection epochs within one stream."
        )
    return status


def _unaccepted_selection(
    event: ControlEvent,
) -> FailedPayload | IncompatiblePayload | ServiceStoppingPayload:
    """Return one typed refusal that preceded durable acceptance."""
    payload = event.payload
    if isinstance(
        payload,
        FailedPayload | IncompatiblePayload | ServiceStoppingPayload,
    ):
        return payload
    raise UnexpectedServiceEventError(
        "The service did not accept account selection."
    )


def _selection_operation(event: ControlEvent) -> OperationId:
    """Return the exact durable operation accepted for selection."""
    payload = event.payload
    if (
        isinstance(payload, AcceptedPayload)
        and payload.operation_id is not None
    ):
        return payload.operation_id
    raise UnexpectedServiceEventError(
        "The service returned an invalid selection acceptance."
    )


def _selection_terminal(
    event: ControlEvent,
    *,
    operation_id: OperationId,
    provider_id: ProviderId,
    account_id: SidekickAccountId,
    pending_epoch: SelectionEpoch | None,
) -> (
    SelectionResult
    | FailedPayload
    | IncompatiblePayload
    | ServiceStoppingPayload
):
    """Return one terminal result bound to the accepted selection."""
    result = event.payload
    if isinstance(result, SelectionResult):
        if pending_epoch is None:
            raise UnexpectedServiceEventError(
                "The service omitted selection epoch status."
            )
        expected_epoch = pending_epoch
        if (
            result.outcome is SelectionOutcome.FAILED_OLD_EPOCH
            and pending_epoch is not None
        ):
            expected_epoch = SelectionEpoch(pending_epoch.value - 1)
        if (
            event.kind is not EventKind.SELECTION_RESULT
            or result.operation_id != operation_id
            or result.provider_id is not provider_id
            or result.target_account_id != account_id
            or (expected_epoch is not None and result.epoch != expected_epoch)
        ):
            raise UnexpectedServiceEventError(
                "The service returned an unrelated selection result."
            )
        return result
    if isinstance(result, FailedPayload) and result.operation_id is None:
        return result
    if isinstance(result, IncompatiblePayload | ServiceStoppingPayload):
        return result
    raise UnexpectedServiceEventError(
        "The service returned an invalid selection result."
    )


def _accepted_operation(
    event: ControlEvent,
    accepted: bool,
    identity: ControlOperationIdentity,
) -> OperationId | None:
    payload = event.payload
    if accepted or not isinstance(payload, AcceptedPayload):
        raise UnexpectedServiceEventError(
            "The service returned an invalid acceptance."
        )
    operation_id = payload.operation_id
    operation_scoped = identity is not ControlOperationIdentity.GLOBAL
    if operation_scoped != (operation_id is not None):
        raise UnexpectedServiceEventError(
            "The service returned an invalid operation identity."
        )
    return operation_id


def _progress_phase(
    event: ControlEvent,
    operation_id: OperationId | None,
) -> ProgressPhase:
    payload = event.payload
    if (
        not isinstance(payload, ProgressPayload)
        or payload.operation_id != operation_id
    ):
        raise UnexpectedServiceEventError(
            "The service returned unrelated progress."
        )
    return payload.phase


def _terminal_payload(
    event: ControlEvent,
    operation_id: OperationId | None,
) -> ControlActionTerminalPayload:
    payload = event.payload
    correlated = (
        isinstance(payload, CompletedPayload | FailedPayload)
        and payload.operation_id == operation_id
    )
    uncorrelated = isinstance(
        payload,
        IncompatiblePayload | ServiceStoppingPayload,
    )
    if correlated or uncorrelated:
        return payload
    raise UnexpectedServiceEventError(
        "The service returned an invalid terminal event."
    )


class ControlClient:
    """One phase-bounded connection to the resident per-user supervisor."""

    def __init__(
        self,
        connection: ConnectedSocket,
        *,
        package_version: str = __version__,
        response_timeout_seconds: float = _LOCAL_RESPONSE_TIMEOUT_SECONDS,
        action_timeout_seconds: float | None = (
            CONTROL_ACTION_TIMEOUT_SECONDS
        ),
    ) -> None:
        if response_timeout_seconds <= 0 or (
            action_timeout_seconds is not None and action_timeout_seconds <= 0
        ):
            raise ValueError("Control timeouts must be positive.")
        connection.settimeout(response_timeout_seconds)
        self._connection = connection
        self._transport = FramedTransport(connection)
        self._package_version = package_version
        self._response_timeout_seconds = response_timeout_seconds
        self._action_timeout_seconds = action_timeout_seconds
        self._handshaken = False
        self._closed = False

    @classmethod
    def connect(
        cls,
        socket_path: Path,
        *,
        package_version: str = __version__,
        connect_timeout_seconds: float = _LOCAL_RESPONSE_TIMEOUT_SECONDS,
        response_timeout_seconds: float = _LOCAL_RESPONSE_TIMEOUT_SECONDS,
        action_timeout_seconds: float | None = (
            CONTROL_ACTION_TIMEOUT_SECONDS
        ),
    ) -> ControlClient:
        """Connect to one qualified Unix socket.

        :raises ServiceCompatibilityError: On native Windows.
        :raises OSError: If the local service cannot be reached.
        """
        if sys.platform == "win32" or not hasattr(socket, "AF_UNIX"):
            raise ServiceCompatibilityError(ProtocolErrorCode.FEATURE_DISABLED)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            endpoint_state = control_endpoint_state(
                socket_path.parent,
                socket_path,
            )
            if endpoint_state is ServiceComponentState.ABSENT:
                raise FileNotFoundError(socket_path)
            if endpoint_state is not ServiceComponentState.HEALTHY:
                raise PermissionError("unsafe_control_endpoint")
            connection.settimeout(connect_timeout_seconds)
            connection.connect(str(socket_path))
            OperatingSystemPeerVerifier().verify(connection)
            return cls(
                connection,
                package_version=package_version,
                response_timeout_seconds=response_timeout_seconds,
                action_timeout_seconds=action_timeout_seconds,
            )
        except OSError, ValueError:
            connection.close()
            raise

    def handshake(self) -> AcceptedPayload:
        """Negotiate exact protocol and package compatibility once."""
        if self._closed:
            raise ConnectionError("The local control client is closed.")
        if self._handshaken:
            return AcceptedPayload(operation_id=None)
        request = self._new_request(RequestKind.HANDSHAKE, EmptyPayload())
        self._transport.send_request(request)
        event = self._transport.receive_event()
        self._validate_event(request, event)
        if event.kind is EventKind.INCOMPATIBLE:
            payload = event.payload
            if not isinstance(payload, IncompatiblePayload):
                raise UnexpectedServiceEventError(
                    "The service sent an invalid negotiation event."
                )
            self.close()
            raise ServiceCompatibilityError(payload.code)
        if event.kind is not EventKind.ACCEPTED:
            self.close()
            raise UnexpectedServiceEventError(
                "The service did not accept the handshake."
            )
        payload = event.payload
        if not isinstance(payload, AcceptedPayload):
            self.close()
            raise UnexpectedServiceEventError(
                "The service sent an invalid handshake response."
            )
        self._handshaken = True
        return payload

    def refresh_account(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
    ) -> Generator[ControlEvent]:
        """Refresh one saved account without selecting it."""
        return self.request(
            RequestKind.REFRESH_ACCOUNT,
            AccountPayload(provider_id, account_id),
        )

    def refresh_all(self) -> Generator[ControlEvent]:
        """Make every account maintenance slot due without selecting it."""
        return self.request(RequestKind.REFRESH_ALL, EmptyPayload())

    def snapshot(self) -> Generator[ControlEvent]:
        """Request one current sanitized service snapshot."""
        return self.request(RequestKind.SNAPSHOT, EmptyPayload())

    def subscribe(self) -> Generator[ControlEvent]:
        """Subscribe to sanitized service events until cancellation."""
        return self.request(RequestKind.SUBSCRIBE, EmptyPayload())

    def register_participant(
        self,
        manifest: ParticipantManifest,
        *,
        protected_endpoint: socket.socket | None = None,
    ) -> Generator[ControlEvent]:
        """Register a peer with one optional protected local endpoint."""
        if protected_endpoint is None:
            return self.request(RequestKind.PARTICIPANT_REGISTER, manifest)
        self.handshake()
        self._connection.settimeout(self._response_timeout_seconds)
        request = self._new_request(
            RequestKind.PARTICIPANT_REGISTER,
            manifest,
        )
        try:
            self._transport.send_request_with_attachment(
                request,
                protected_endpoint,
            )
        finally:
            protected_endpoint.close()
        return self._event_stream(request)

    def subscribe_participant(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
    ) -> Generator[ControlEvent]:
        """Subscribe one registered participant to admission notices."""
        return self.request(
            RequestKind.PARTICIPANT_SUBSCRIBE,
            ParticipantConnectionRequest(
                participant_id,
                connection_generation,
            ),
        )

    def begin_turn(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        turn_id: TurnId,
    ) -> Generator[ControlEvent]:
        """Request one exact turn admission boundary."""
        return self.request(
            RequestKind.TURN_BEGIN,
            TurnBeginRequest(
                participant_id,
                connection_generation,
                turn_id,
            ),
        )

    def end_turn(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        turn_id: TurnId,
    ) -> Generator[ControlEvent]:
        """Close one exact admitted turn lease."""
        return self.request(
            RequestKind.TURN_END,
            TurnEndRequest(
                participant_id,
                connection_generation,
                turn_id,
            ),
        )

    def participant_ready(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        proof: ParticipantReadyProof,
    ) -> Generator[ControlEvent]:
        """Acknowledge exact next-turn authority readiness."""
        return self.request(
            RequestKind.PARTICIPANT_READY,
            ParticipantReadyRequest(
                participant_id=participant_id,
                connection_generation=connection_generation,
                proof=proof,
            ),
        )

    def participant_adopted(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        proof: ParticipantAdoptionProof,
    ) -> Generator[ControlEvent]:
        """Report first-real-turn adoption of an exact authority."""
        return self.request(
            RequestKind.PARTICIPANT_ADOPT,
            ParticipantAdoptionRequest(
                participant_id=participant_id,
                connection_generation=connection_generation,
                proof=proof,
            ),
        )

    def select_account(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
    ) -> Generator[ControlEvent]:
        """Select one saved account through global coordination."""
        return self.request(
            RequestKind.SELECT_ACCOUNT,
            AccountPayload(provider_id, account_id),
        )

    def selection_status(
        self,
        provider_id: ProviderId,
    ) -> Generator[ControlEvent]:
        """Read one provider's secret-free selection snapshot."""
        return self.request(
            RequestKind.SELECTION_STATUS,
            ProviderPayload(provider_id),
        )

    def reconcile(
        self,
        provider_id: ProviderId,
    ) -> Generator[ControlEvent]:
        """Request provider read-back reconciliation."""
        return self.request(
            RequestKind.RECONCILE,
            ProviderPayload(provider_id),
        )

    def shutdown(self) -> Generator[ControlEvent]:
        """Ask the resident service to stop cleanly."""
        return self.request(RequestKind.SHUTDOWN, EmptyPayload())

    def request(
        self,
        kind: RequestKind,
        payload: RequestPayload,
    ) -> Generator[ControlEvent]:
        """Send one closed action only after a successful handshake."""
        if kind is RequestKind.HANDSHAKE:
            raise ValueError("Use handshake() for protocol negotiation.")
        self.handshake()
        self._connection.settimeout(self._response_timeout_seconds)
        request = self._new_request(kind, payload)
        self._transport.send_request(request)
        return self._event_stream(request)

    def close(self) -> None:
        """Close this client and cancel any in-flight event stream."""
        if self._closed:
            return
        self._closed = True
        self._transport.close()

    def _new_request(
        self,
        kind: RequestKind,
        payload: RequestPayload,
    ) -> ControlRequest:
        return ControlRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=new_request_id(),
            kind=kind,
            payload=payload,
            package_version=self._package_version,
        )

    def _accepted_stream_timeout(self, kind: RequestKind) -> float | None:
        if kind in {
            RequestKind.SUBSCRIBE,
            RequestKind.PARTICIPANT_SUBSCRIBE,
        }:
            return None
        return self._action_timeout_seconds

    def _event_stream(
        self,
        request: ControlRequest,
    ) -> Generator[ControlEvent]:
        first_event = True
        terminal = False
        try:
            while True:
                event = self._transport.receive_event()
                self._validate_event(request, event)
                if (
                    first_event
                    and event.kind is EventKind.ACCEPTED
                    and request.kind in _EXTENDED_STREAM_REQUEST_KINDS
                ):
                    self._connection.settimeout(
                        self._accepted_stream_timeout(request.kind)
                    )
                first_event = False
                yield event
                if event.kind in {
                    EventKind.COMPLETED,
                    EventKind.FAILED,
                    EventKind.INCOMPATIBLE,
                    EventKind.SERVICE_STOPPING,
                    EventKind.SNAPSHOT,
                    EventKind.PARTICIPANT_REGISTERED,
                    EventKind.TURN_ADMISSION,
                    EventKind.SELECTION_RESULT,
                } or (
                    event.kind is EventKind.SELECTION_STATUS
                    and request.kind is RequestKind.SELECTION_STATUS
                ):
                    terminal = True
                    return
        finally:
            if not terminal:
                self.close()

    def _validate_event(
        self,
        request: ControlRequest,
        event: ControlEvent,
    ) -> None:
        if event.request_id != request.request_id:
            self.close()
            raise UnexpectedServiceEventError(
                "The service returned an unrelated request ID."
            )
        if event.protocol_version != PROTOCOL_VERSION:
            self.close()
            raise ServiceCompatibilityError(
                ProtocolErrorCode.INCOMPATIBLE_PROTOCOL
            )
        if event.package_version != self._package_version:
            self.close()
            raise ServiceCompatibilityError(
                ProtocolErrorCode.INCOMPATIBLE_VERSION
            )

"""Same-user supervisor control client."""

import socket
import sys
from collections.abc import Generator
from pathlib import Path

from sidekick_usages import __version__
from sidekick_usages.core.accounts.identifiers import new_request_id
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.protocol import (
    PROTOCOL_VERSION,
    FramedTransport,
)
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    AccountPayload,
    ActivationPayload,
    ControlEvent,
    ControlRequest,
    EmptyPayload,
    IncompatiblePayload,
    ProviderPayload,
    RequestPayload,
)
from sidekick_usages.daemon.types.protocol import (
    ConnectedSocket,
    EventKind,
    ProtocolErrorCode,
    RequestKind,
)

CONTROL_ACTION_TIMEOUT_SECONDS = 125.0
_LOCAL_RESPONSE_TIMEOUT_SECONDS = 5.0
_LONG_ACTION_REQUEST_KINDS = frozenset(
    {
        RequestKind.ACTIVATE,
        RequestKind.RECONCILE,
        RequestKind.REFRESH_ACCOUNT,
    }
)
_EXTENDED_STREAM_REQUEST_KINDS = (
    _LONG_ACTION_REQUEST_KINDS | {RequestKind.SUBSCRIBE}
)


class ServiceCompatibilityError(ConnectionError):
    """The installed client and resident service cannot safely cooperate."""

    def __init__(self, code: ProtocolErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class UnexpectedServiceEventError(ConnectionError):
    """The service broke request correlation or event ordering."""


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
            action_timeout_seconds is not None
            and action_timeout_seconds <= 0
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
            connection.settimeout(connect_timeout_seconds)
            connection.connect(str(socket_path))
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

    def activate(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
        *,
        allow_remote_control_disconnect: bool = False,
    ) -> Generator[ControlEvent]:
        """Activate one stable saved account after compatibility proof."""
        return self.request(
            RequestKind.ACTIVATE,
            ActivationPayload(
                provider_id,
                account_id,
                allow_remote_control_disconnect,
            ),
        )

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
        if kind is RequestKind.SUBSCRIBE:
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
                }:
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

"""Ephemeral callback exchange over one inherited Unix socketpair."""

import os
import selectors
import socket
import time
from collections.abc import Callable
from contextlib import suppress
from threading import Event, Lock

from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.daemon.models.worker import (
    CALLBACK_DESCRIPTOR_ENVIRONMENT_KEY,
    MINIMUM_CALLBACK_DESCRIPTOR,
    CallbackExchangeRegistration,
)
from sidekick_usages.daemon.types.worker import (
    CallbackExchangePhase,
    CallbackExchangeState,
)
from sidekick_usages.serialization.framing import (
    BoundedFrameDecoder,
    FramingError,
    clear_mutable_buffer,
    encode_bounded_frame,
)

MAX_CALLBACK_FRAME_BYTES = 512 * 1024
CALLBACK_INSTRUCTION_TIMEOUT_SECONDS = 8.0
CALLBACK_COMPLETION_TAIL_SECONDS = 2.0
_READ_CHUNK_BYTES = 64 * 1024
_CANCELLATION_POLL_SECONDS = 0.1


class CallbackExchangeError(RuntimeError):
    """A callback exchange failed without exposing its payload."""


class SupervisorCallbackExchange:
    """Supervisor-owned endpoint for one exact callback operation."""

    def __init__(
        self,
        operation_id: OperationId,
        instruction: bytes,
        response_deadline: float,
        completion_deadline: float,
        parent: socket.socket,
        child: socket.socket,
        monotonic: Callable[[], float],
    ) -> None:
        if (
            response_deadline <= monotonic()
            or completion_deadline
            < response_deadline + CALLBACK_COMPLETION_TAIL_SECONDS
        ):
            raise ValueError("Callback deadlines are invalid.")
        if not instruction or len(instruction) > MAX_CALLBACK_FRAME_BYTES:
            raise ValueError("Callback instruction is outside the bound.")
        self.operation_id = operation_id
        self.response_deadline = response_deadline
        self.completion_deadline = completion_deadline
        self._instruction = bytearray(instruction)
        self._parent = parent
        self._child = child
        self._monotonic = monotonic
        self._lock = Lock()
        self._terminal = Event()
        self._claimed = False
        self._started = False
        self._closed = False
        self._state = CallbackExchangeState.AWAITING_RESPONSE

    @property
    def child_descriptor(self) -> int:
        """Return the claimed not-yet-launched worker descriptor."""
        with self._lock:
            if not self._claimed or self._started or self._closed:
                raise CallbackExchangeError
            descriptor = self._child.fileno()
        if descriptor < MINIMUM_CALLBACK_DESCRIPTOR:
            raise CallbackExchangeError
        return descriptor

    def claim(self) -> None:
        """Claim this exchange for exactly one worker launch."""
        with self._lock:
            if self._claimed or self._closed:
                raise CallbackExchangeError
            self._claimed = True

    def worker_started(self) -> None:
        """Release the duplicate child endpoint and send one instruction."""
        with self._lock:
            if not self._claimed or self._started or self._closed:
                raise CallbackExchangeError
            self._started = True
        self._child.close()
        try:
            _send_frame(
                self._parent,
                self._instruction,
                self.response_deadline,
                self._monotonic,
            )
        finally:
            clear_mutable_buffer(self._instruction)

    def receive_response(self) -> bytearray:
        """Read exactly one worker response followed by phase EOF."""
        with self._lock:
            if (
                self._closed
                or self._state is not CallbackExchangeState.AWAITING_RESPONSE
            ):
                raise CallbackExchangeError
        return _receive_one_frame(
            self._parent,
            self.response_deadline,
            self._monotonic,
            require_eof=True,
            cancelled=self._terminal.is_set,
        )

    def acknowledge(self, payload: bytes | bytearray) -> None:
        """Send one correlated ACK and close the supervisor write phase."""
        with self._lock:
            if (
                not self._started
                or self._closed
                or self._state is not CallbackExchangeState.AWAITING_RESPONSE
            ):
                raise CallbackExchangeError
            self._state = CallbackExchangeState.AWAITING_COMPLETION
        try:
            _send_frame(
                self._parent,
                payload,
                self.completion_deadline,
                self._monotonic,
            )
            self._parent.shutdown(socket.SHUT_WR)
        except OSError:
            self.complete(False)
            raise CallbackExchangeError from None
        except CallbackExchangeError:
            self.complete(False)
            raise

    def wait_for_completion(self) -> bool:
        """Wait for scheduler-confirmed callback commit and cleanup."""
        remaining = self.completion_deadline - self._monotonic()
        if remaining <= 0 or not self._terminal.wait(remaining):
            return False
        with self._lock:
            return self._state is CallbackExchangeState.COMPLETED

    def complete(self, succeeded: bool) -> None:
        """Publish one secret-free terminal scheduler outcome."""
        state = (
            CallbackExchangeState.COMPLETED
            if succeeded
            else CallbackExchangeState.CANCELLED
        )
        with self._lock:
            if self._state in {
                CallbackExchangeState.COMPLETED,
                CallbackExchangeState.CANCELLED,
            }:
                return
            self._state = state
            self._closed = True
        self._terminal.set()
        self._close_endpoints()

    def cancel_if_awaiting_response(self) -> bool:
        """Cancel only before response dispatch begins commit completion."""
        with self._lock:
            if self._state is CallbackExchangeState.AWAITING_COMPLETION:
                return False
            if self._state in {
                CallbackExchangeState.COMPLETED,
                CallbackExchangeState.CANCELLED,
            }:
                return True
        self.complete(False)
        return True

    def close(self) -> None:
        """Cancel both phases and clear retained instruction bytes."""
        self.complete(False)

    def _close_endpoints(self) -> None:
        clear_mutable_buffer(self._instruction)
        for endpoint in (self._parent, self._child):
            with suppress(OSError):
                endpoint.shutdown(socket.SHUT_RDWR)
            with suppress(OSError):
                endpoint.close()


class CallbackExchangeRegistry:
    """Bind live callback exchanges to exact durable operation IDs."""

    def __init__(
        self,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._monotonic = monotonic
        self._lock = Lock()
        self._registrations: dict[
            OperationId,
            CallbackExchangeRegistration[SupervisorCallbackExchange],
        ] = {}
        self._closing = False

    def create(
        self,
        operation_id: OperationId,
        instruction: bytes,
        response_deadline: float,
        completion_deadline: float,
    ) -> SupervisorCallbackExchange:
        """Create the sole live callback exchange."""
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        parent.set_inheritable(False)
        child.set_inheritable(False)
        try:
            exchange = SupervisorCallbackExchange(
                operation_id,
                instruction,
                response_deadline,
                completion_deadline,
                parent,
                child,
                self._monotonic,
            )
            with self._lock:
                if self._closing or self._registrations:
                    raise CallbackExchangeError
                self._registrations[operation_id] = (
                    CallbackExchangeRegistration(exchange)
                )
        except Exception:
            parent.close()
            child.close()
            raise
        return exchange

    def available(
        self,
        operation_id: OperationId,
    ) -> bool:
        """Return whether one live exchange is available to launch."""
        with self._lock:
            registration = self._registrations.get(operation_id)
            return (
                registration is not None
                and registration.phase is CallbackExchangePhase.READY
            )

    def claim(
        self,
        operation_id: OperationId,
    ) -> SupervisorCallbackExchange | None:
        """Claim one exact exchange for a worker launch."""
        with self._lock:
            registration = self._registrations.get(operation_id)
            if (
                registration is None
                or registration.phase is not CallbackExchangePhase.READY
            ):
                return None
            exchange = registration.exchange
            exchange.claim()
            registration.phase = CallbackExchangePhase.CLAIMED
            return exchange

    def finish_launch(self, operation_id: OperationId) -> bool:
        """Finish one descriptor claim or honor concurrent cancellation."""
        exchange: SupervisorCallbackExchange | None = None
        with self._lock:
            registration = self._registrations.get(operation_id)
            if (
                registration is None
                or registration.phase is not CallbackExchangePhase.CLAIMED
            ):
                return False
            if registration.cancellation_requested:
                exchange = registration.exchange
                self._registrations.pop(operation_id, None)
            else:
                registration.phase = CallbackExchangePhase.STARTED
                return True
        if exchange is not None:
            exchange.close()
        return False

    def abort_launch(self, operation_id: OperationId) -> None:
        """Forget and close an exchange whose worker did not launch."""
        with self._lock:
            registration = self._registrations.pop(operation_id, None)
        if registration is not None:
            registration.exchange.close()

    def complete(self, operation_id: OperationId, succeeded: bool) -> None:
        """Publish terminal scheduler outcome and forget the exchange."""
        with self._lock:
            registration = self._registrations.pop(operation_id, None)
        if registration is not None:
            registration.exchange.complete(succeeded)

    def cancel(self, operation_id: OperationId) -> None:
        """Cancel and forget one exchange regardless of its phase."""
        registration: (
            CallbackExchangeRegistration[SupervisorCallbackExchange] | None
        )
        with self._lock:
            registration = self._registrations.get(operation_id)
            if (
                registration is not None
                and registration.phase is CallbackExchangePhase.CLAIMED
            ):
                registration.cancellation_requested = True
                return
            registration = self._registrations.pop(operation_id, None)
        if registration is not None:
            registration.exchange.close()

    def cancel_if_awaiting_response(
        self,
        operation_id: OperationId,
    ) -> bool:
        """Cancel only a callback that has not entered commit completion."""
        registration: (
            CallbackExchangeRegistration[SupervisorCallbackExchange] | None
        )
        with self._lock:
            registration = self._registrations.get(operation_id)
            if registration is None:
                return True
            if registration.phase is CallbackExchangePhase.CLAIMED:
                registration.cancellation_requested = True
                return True
            cancelled = registration.exchange.cancel_if_awaiting_response()
            if cancelled:
                self._registrations.pop(operation_id, None)
            return cancelled

    def close(self) -> None:
        """Cancel and forget every live exchange."""
        exchanges: list[SupervisorCallbackExchange] = []
        with self._lock:
            self._closing = True
            for operation_id, registration in tuple(
                self._registrations.items()
            ):
                if registration.phase is CallbackExchangePhase.CLAIMED:
                    registration.cancellation_requested = True
                    continue
                exchanges.append(registration.exchange)
                self._registrations.pop(operation_id, None)
        for exchange in exchanges:
            exchange.close()


class WorkerCallbackSubmission:
    """Token-free worker handle waiting for one supervisor ACK."""

    def __init__(
        self,
        endpoint: socket.socket,
        completion_deadline: float,
        monotonic: Callable[[], float],
    ) -> None:
        self._endpoint = endpoint
        self._completion_deadline = completion_deadline
        self._monotonic = monotonic

    def receive_acknowledgement(self) -> bytearray:
        """Read exactly one ACK followed by supervisor phase EOF."""
        return _receive_one_frame(
            self._endpoint,
            self._completion_deadline,
            self._monotonic,
            require_eof=True,
        )


class WorkerCallbackChannel:
    """Worker-owned endpoint adopted from the trusted launcher."""

    def __init__(
        self,
        endpoint: socket.socket,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._endpoint = endpoint
        self._monotonic = monotonic
        self._closed = False

    @classmethod
    def from_environment(cls) -> WorkerCallbackChannel:
        """Adopt and de-inherit the sole launcher-owned descriptor."""
        raw_descriptor = os.environ.pop(
            CALLBACK_DESCRIPTOR_ENVIRONMENT_KEY,
            None,
        )
        if (
            raw_descriptor is None
            or not raw_descriptor.isascii()
            or not raw_descriptor.isdecimal()
        ):
            raise CallbackExchangeError
        descriptor = int(raw_descriptor)
        if descriptor < MINIMUM_CALLBACK_DESCRIPTOR:
            raise CallbackExchangeError
        try:
            os.set_inheritable(descriptor, False)
            endpoint = socket.socket(fileno=descriptor)
        except OSError:
            raise CallbackExchangeError from None
        return cls(endpoint)

    def receive_instruction(self) -> bytearray:
        """Read the sole supervisor instruction before worker execution."""
        deadline = self._monotonic() + CALLBACK_INSTRUCTION_TIMEOUT_SECONDS
        return _receive_one_frame(
            self._endpoint,
            deadline,
            self._monotonic,
            require_eof=False,
        )

    def submit(
        self,
        payload: bytearray,
        response_deadline: float,
        completion_deadline: float,
    ) -> WorkerCallbackSubmission:
        """Send one response, clear it, and close the worker write phase."""
        if self._closed:
            raise CallbackExchangeError
        try:
            _send_frame(
                self._endpoint,
                payload,
                response_deadline,
                self._monotonic,
            )
            self._endpoint.shutdown(socket.SHUT_WR)
        except OSError:
            raise CallbackExchangeError from None
        finally:
            clear_mutable_buffer(payload)
        return WorkerCallbackSubmission(
            self._endpoint,
            completion_deadline,
            self._monotonic,
        )

    def close(self) -> None:
        """Close the inherited endpoint exactly once."""
        if self._closed:
            return
        self._closed = True
        with suppress(OSError):
            self._endpoint.shutdown(socket.SHUT_RDWR)
        self._endpoint.close()


def _send_frame(
    endpoint: socket.socket,
    payload: bytes | bytearray,
    deadline: float,
    monotonic: Callable[[], float],
) -> None:
    frame: bytearray | None = None
    pending: memoryview | None = None
    selector: selectors.BaseSelector | None = None
    try:
        frame = encode_bounded_frame(payload, MAX_CALLBACK_FRAME_BYTES)
        selector = selectors.DefaultSelector()
        pending = memoryview(frame)
        selector.register(endpoint, selectors.EVENT_WRITE)
        while pending:
            remaining = deadline - monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise CallbackExchangeError
            try:
                written = endpoint.send(pending, socket.MSG_DONTWAIT)
            except BlockingIOError, InterruptedError:
                continue
            except OSError:
                raise CallbackExchangeError from None
            if written == 0:
                raise CallbackExchangeError
            pending = pending[written:]
    except FramingError:
        raise CallbackExchangeError from None
    finally:
        if pending is not None:
            pending.release()
        if selector is not None:
            selector.close()
        if frame is not None:
            clear_mutable_buffer(frame)


def _receive_one_frame(
    endpoint: socket.socket,
    deadline: float,
    monotonic: Callable[[], float],
    *,
    require_eof: bool,
    cancelled: Callable[[], bool] | None = None,
) -> bytearray:
    decoder = BoundedFrameDecoder(MAX_CALLBACK_FRAME_BYTES)
    selector = selectors.DefaultSelector()
    scratch = bytearray(_READ_CHUNK_BYTES)
    frame: bytearray | None = None
    try:
        selector.register(endpoint, selectors.EVENT_READ)
        while True:
            if not _wait_readable(
                selector,
                deadline,
                monotonic,
                cancelled,
            ):
                raise CallbackExchangeError
            received = _receive_chunk(endpoint, scratch)
            if received is None:
                continue
            if received == 0:
                return _finished_frame(decoder, frame)
            try:
                frame = _next_frame(
                    decoder,
                    frame,
                    memoryview(scratch)[:received],
                )
            finally:
                _zero(scratch, received)
            if frame is not None and not require_eof:
                if decoder.pending:
                    raise CallbackExchangeError
                return frame
    except Exception:
        if frame is not None:
            clear_mutable_buffer(frame)
        decoder.clear()
        raise
    finally:
        clear_mutable_buffer(scratch)
        selector.close()


def _wait_readable(
    selector: selectors.BaseSelector,
    deadline: float,
    monotonic: Callable[[], float],
    cancelled: Callable[[], bool] | None,
) -> bool:
    while cancelled is None or not cancelled():
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        wait_seconds = (
            remaining
            if cancelled is None
            else min(remaining, _CANCELLATION_POLL_SECONDS)
        )
        if selector.select(wait_seconds):
            return True
        if cancelled is None:
            return False
    return False


def _receive_chunk(
    endpoint: socket.socket,
    scratch: bytearray,
) -> int | None:
    try:
        return endpoint.recv_into(
            scratch,
            len(scratch),
            socket.MSG_DONTWAIT,
        )
    except BlockingIOError, InterruptedError:
        return None
    except OSError:
        raise CallbackExchangeError from None


def _next_frame(
    decoder: BoundedFrameDecoder,
    current: bytearray | None,
    chunk: memoryview,
) -> bytearray | None:
    try:
        frames = decoder.feed(chunk)
    except FramingError:
        raise CallbackExchangeError from None
    if len(frames) > 1 or (current is not None and frames):
        for duplicate in frames:
            clear_mutable_buffer(duplicate)
        raise CallbackExchangeError
    return current if not frames else frames[0]


def _finished_frame(
    decoder: BoundedFrameDecoder,
    frame: bytearray | None,
) -> bytearray:
    try:
        decoder.finish()
    except FramingError:
        raise CallbackExchangeError from None
    if frame is None:
        raise CallbackExchangeError
    return frame


def _zero(payload: bytearray, length: int) -> None:
    payload[:length] = b"\x00" * length

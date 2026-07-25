"""Singleton resident responder for shared-daemon external auth."""

import time
from collections.abc import Callable
from threading import Event, Lock, Thread

from sidekick_usages.core.accounts.identifiers import new_operation_id
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.models import (
    JsonRpcServerRequest,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.external_auth.refresh import (
    CODEX_REFRESH_METHOD,
    decode_codex_refresh_reply,
    decode_codex_refresh_request,
    encode_codex_callback_acknowledgement,
    encode_codex_callback_instruction,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexCallbackAcknowledgement,
    CodexCallbackInstruction,
    CodexProjectionExpectation,
    CodexProjectionReceipt,
    CodexRefreshReplyLease,
    CodexRefreshRequest,
)
from sidekick_usages.providers.codex.broker.ports import (
    CodexCallbackDispatcher,
    CodexCallbackExchange,
    CodexSelectionReader,
)
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime
from sidekick_usages.providers.codex.broker.types import (
    CodexBrokerFailure,
    CodexCallbackMode,
)
from sidekick_usages.serialization.framing import clear_mutable_buffer

CODEX_CALLBACK_RESPONSE_SECONDS = 8.0
CODEX_CALLBACK_COMPLETION_SECONDS = 10.0
CODEX_CALLBACK_ERROR_RESERVE_SECONDS = 0.5
_BROKER_RECEIVE_SECONDS = 0.25
_BROKER_RECONNECT_INITIAL_SECONDS = 0.25
_BROKER_RECONNECT_MAX_SECONDS = 2.0
_BROKER_JOIN_SECONDS = 3.0
_NANOSECONDS_PER_SECOND = 1_000_000_000


class CodexRefreshBroker:
    """Own one daemon connection and one callback at a time."""

    def __init__(
        self,
        runtime_factory: Callable[
            [Callable[[], bool]],
            CodexSharedRuntime,
        ],
        selection: CodexSelectionReader,
        callbacks: CodexCallbackDispatcher,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        status_changed: Callable[[], None] | None = None,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._selection = selection
        self._callbacks = callbacks
        self._monotonic = monotonic
        self._status_changed = status_changed
        self._stop = Event()
        self._ready = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._active_operation: OperationId | None = None

    @property
    def ready(self) -> bool:
        """Return whether the selected projection can be refreshed."""
        return self._ready.is_set()

    def start(self) -> None:
        """Start the sole daemon owner thread exactly once."""
        with self._lock:
            if self._thread is not None:
                raise RuntimeError("Codex refresh broker already started.")
            thread = Thread(
                target=self._run,
                daemon=True,
                name="sidekick-codex-broker",
            )
            self._thread = thread
        thread.start()

    def request_stop(self) -> None:
        """Reject new callbacks and cancel work before response dispatch."""
        with self._lock:
            self._stop.set()
            operation_id = self._active_operation
        self._set_ready(False)
        if operation_id is not None:
            self._callbacks.cancel(operation_id)

    def close(self) -> None:
        """Join the stopped daemon owner and release its connection."""
        try:
            self.request_stop()
        finally:
            with self._lock:
                thread = self._thread
            if thread is not None:
                thread.join(timeout=_BROKER_JOIN_SECONDS)
                if thread.is_alive():
                    raise RuntimeError("Codex refresh broker did not stop.")

    def _run(self) -> None:
        runtime: CodexSharedRuntime | None = None
        reconnect_seconds = _BROKER_RECONNECT_INITIAL_SECONDS
        try:
            while not self._stop.is_set():
                try:
                    expectation = self._expectation()
                    if expectation is None:
                        runtime = _drop_runtime(runtime)
                        self._set_ready(True)
                        reconnect_seconds = _BROKER_RECONNECT_INITIAL_SECONDS
                        self._stop.wait(_BROKER_RECEIVE_SECONDS)
                        continue
                    if runtime is None:
                        self._set_ready(False)
                        runtime = self._runtime_factory(self._stop.is_set)
                    receipt = runtime.receipt
                    if receipt is None or not _receipt_matches(
                        receipt,
                        expectation,
                    ):
                        self._set_ready(False)
                        receipt = runtime.prepare(
                            expectation.account_id,
                            expectation.provider_identity,
                            expectation.generation,
                        )
                    if receipt is None:
                        receipt = self._rehydrate(runtime, expectation)
                    self._set_ready(True)
                    reconnect_seconds = _BROKER_RECONNECT_INITIAL_SECONDS
                    self._receive(runtime, expectation, receipt)
                except CodexAppServerError as error:
                    if error.code is CodexAppServerFailure.PROTOCOL_TIMEOUT:
                        continue
                    self._set_ready(False)
                    runtime = _drop_runtime(runtime)
                    self._stop.wait(reconnect_seconds)
                    reconnect_seconds = min(
                        reconnect_seconds * 2,
                        _BROKER_RECONNECT_MAX_SECONDS,
                    )
                except CodexBrokerError, OSError, RuntimeError, ValueError:
                    self._set_ready(False)
                    runtime = _drop_runtime(runtime)
                    self._stop.wait(reconnect_seconds)
                    reconnect_seconds = min(
                        reconnect_seconds * 2,
                        _BROKER_RECONNECT_MAX_SECONDS,
                    )
        finally:
            self._set_ready(False)
            _drop_runtime(runtime)

    def _set_ready(self, ready: bool) -> None:
        if ready and self._stop.is_set():
            ready = False
        current = self._ready.is_set()
        if current == ready:
            return
        if ready:
            self._ready.set()
        else:
            self._ready.clear()
        if self._status_changed is not None:
            self._status_changed()

    def _receive(
        self,
        runtime: CodexSharedRuntime,
        expectation: CodexProjectionExpectation,
        receipt: CodexProjectionReceipt,
    ) -> None:
        message = runtime.receive(timeout_seconds=_BROKER_RECEIVE_SECONDS)
        if not isinstance(message, JsonRpcServerRequest):
            return
        if message.method != CODEX_REFRESH_METHOD:
            return
        started_at = self._monotonic()
        official_deadline = started_at + CODEX_CALLBACK_RESPONSE_SECONDS
        response_deadline = (
            official_deadline - CODEX_CALLBACK_ERROR_RESERVE_SECONDS
        )
        completion_deadline = started_at + CODEX_CALLBACK_COMPLETION_SECONDS
        try:
            request = decode_codex_refresh_request(message)
        except CodexBrokerError:
            self._reject(runtime, message, official_deadline)
            return
        if (
            request.previous_provider_identity != expectation.provider_identity
            or not _receipt_matches(receipt, expectation)
            or self._expectation() != expectation
        ):
            self._reject(runtime, message, official_deadline)
            return
        self._refresh(
            runtime,
            expectation,
            request,
            response_deadline,
            official_deadline,
            completion_deadline,
        )

    def _rehydrate(
        self,
        runtime: CodexSharedRuntime,
        expectation: CodexProjectionExpectation,
    ) -> CodexProjectionReceipt:
        started_at = self._monotonic()
        response_deadline = (
            started_at
            + CODEX_CALLBACK_RESPONSE_SECONDS
            - CODEX_CALLBACK_ERROR_RESERVE_SECONDS
        )
        completion_deadline = started_at + CODEX_CALLBACK_COMPLETION_SECONDS
        instruction, exchange = self._dispatch(
            expectation,
            CodexCallbackMode.REHYDRATE,
            response_deadline,
            completion_deadline,
        )
        completed = False
        try:
            reply = self._reply(exchange, instruction)
            with reply:
                receipt = runtime.install(
                    reply,
                    deadline=(
                        completion_deadline
                        - CODEX_CALLBACK_ERROR_RESERVE_SECONDS
                    ),
                )
            exchange.acknowledge(
                encode_codex_callback_acknowledgement(
                    CodexCallbackAcknowledgement(
                        instruction.operation_id,
                        instruction.mode,
                        reply.generation,
                    )
                )
            )
            if not exchange.wait_for_completion():
                raise RuntimeError("Codex rehydration did not commit.")
            if self._expectation() != CodexProjectionExpectation(
                reply.account_id,
                reply.provider_identity,
                reply.generation,
            ):
                raise RuntimeError("Codex rehydration state is inconsistent.")
            completed = True
            return receipt
        finally:
            self._finish_dispatch(instruction.operation_id, completed)

    def _refresh(
        self,
        runtime: CodexSharedRuntime,
        expectation: CodexProjectionExpectation,
        request: CodexRefreshRequest,
        response_deadline: float,
        official_deadline: float,
        completion_deadline: float,
    ) -> None:
        instruction: CodexCallbackInstruction | None = None
        completed = False
        responded = False
        try:
            instruction, exchange = self._dispatch(
                expectation,
                CodexCallbackMode.REFRESH,
                response_deadline,
                completion_deadline,
            )
            reply = self._reply(exchange, instruction)
            if runtime.receipt is None or not _receipt_matches(
                runtime.receipt, expectation
            ):
                raise CodexBrokerError(CodexBrokerFailure.RUNTIME_CHANGED)
            remaining = official_deadline - self._monotonic()
            if remaining <= 0:
                raise CodexAppServerError(
                    CodexAppServerFailure.PROTOCOL_TIMEOUT
                )
            with reply:
                runtime.respond_refresh(
                    request.request_id,
                    reply,
                    timeout_seconds=remaining,
                )
            responded = True
            exchange.acknowledge(
                encode_codex_callback_acknowledgement(
                    CodexCallbackAcknowledgement(
                        instruction.operation_id,
                        instruction.mode,
                        reply.generation,
                    )
                )
            )
            if not exchange.wait_for_completion():
                raise RuntimeError("Codex refresh did not commit.")
            if self._expectation() != CodexProjectionExpectation(
                reply.account_id,
                reply.provider_identity,
                reply.generation,
            ):
                raise RuntimeError("Codex refresh state is inconsistent.")
            completed = True
        except (
            CodexAppServerError,
            CodexBrokerError,
            RuntimeError,
            ValueError,
        ):
            if not responded:
                self._reject_request(runtime, request, official_deadline)
            raise
        finally:
            if instruction is not None:
                self._finish_dispatch(
                    instruction.operation_id,
                    completed,
                )

    def _dispatch(
        self,
        expectation: CodexProjectionExpectation,
        mode: CodexCallbackMode,
        response_deadline: float,
        completion_deadline: float,
    ) -> tuple[CodexCallbackInstruction, CodexCallbackExchange]:
        operation_id = new_operation_id()
        instruction = CodexCallbackInstruction(
            operation_id,
            mode,
            expectation.account_id,
            expectation.provider_identity,
            expectation.generation,
            int(response_deadline * _NANOSECONDS_PER_SECOND),
            int(completion_deadline * _NANOSECONDS_PER_SECOND),
        )
        encoded = encode_codex_callback_instruction(instruction)
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("Codex refresh broker is stopping.")
            if self._active_operation is not None:
                raise RuntimeError("Codex callback is already active.")
            self._active_operation = operation_id
            try:
                exchange = self._callbacks.dispatch(
                    operation_id,
                    expectation.account_id,
                    encoded,
                    response_deadline,
                    completion_deadline,
                )
            except Exception:
                self._active_operation = None
                raise
        return instruction, exchange

    def _reply(
        self,
        exchange: CodexCallbackExchange,
        instruction: CodexCallbackInstruction,
    ) -> CodexRefreshReplyLease:
        payload = exchange.receive_response()
        try:
            return decode_codex_refresh_reply(payload, instruction)
        finally:
            clear_mutable_buffer(payload)

    def _finish_dispatch(
        self,
        operation_id: OperationId,
        completed: bool,
    ) -> None:
        try:
            if not completed:
                self._callbacks.cancel(operation_id)
        finally:
            self._clear_active(operation_id)

    def _clear_active(self, operation_id: OperationId) -> None:
        with self._lock:
            if self._active_operation == operation_id:
                self._active_operation = None

    def _expectation(self) -> CodexProjectionExpectation | None:
        selected = self._selection.current()
        if (
            selected is None
            or selected.provider_id is not ProviderId.CODEX
            or selected.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
            or selected.account_id is None
            or selected.provider_identity is None
            or selected.runtime_generation is None
        ):
            return None
        return CodexProjectionExpectation(
            selected.account_id,
            selected.provider_identity,
            selected.runtime_generation,
        )

    def _reject(
        self,
        runtime: CodexSharedRuntime,
        request: JsonRpcServerRequest,
        deadline: float,
    ) -> None:
        request_id = request.request_id
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            return
        self._reject_request_id(runtime, request_id, deadline)

    def _reject_request(
        self,
        runtime: CodexSharedRuntime,
        request: CodexRefreshRequest,
        deadline: float,
    ) -> None:
        self._reject_request_id(runtime, request.request_id, deadline)

    def _reject_request_id(
        self,
        runtime: CodexSharedRuntime,
        request_id: int,
        deadline: float,
    ) -> None:
        remaining = deadline - self._monotonic()
        if remaining > 0:
            runtime.reject_refresh(
                request_id,
                timeout_seconds=remaining,
            )


def _drop_runtime(
    runtime: CodexSharedRuntime | None,
) -> None:
    if runtime is not None:
        runtime.close()


def _receipt_matches(
    receipt: CodexProjectionReceipt,
    expectation: CodexProjectionExpectation,
) -> bool:
    return (
        receipt.account_id == expectation.account_id
        and receipt.provider_identity == expectation.provider_identity
        and receipt.generation == expectation.generation
    )

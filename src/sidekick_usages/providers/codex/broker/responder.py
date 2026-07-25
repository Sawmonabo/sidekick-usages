"""Singleton resident responder for shared-daemon external auth."""

import time
from collections.abc import Callable
from threading import Event, Lock, Thread

from sidekick_usages.core.accounts.identifiers import new_operation_id
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    ProviderRuntimeState,
)
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
from sidekick_usages.providers.codex.broker.external_auth.activation import (
    decode_codex_activation_reply,
    encode_codex_activation_acknowledgement,
    encode_codex_activation_instruction,
)
from sidekick_usages.providers.codex.broker.external_auth.refresh import (
    CODEX_REFRESH_METHOD,
    decode_codex_refresh_reply,
    decode_codex_refresh_request,
    encode_codex_callback_acknowledgement,
    encode_codex_callback_instruction,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexActivationAcknowledgement,
    CodexActivationInstruction,
    CodexCallbackAcknowledgement,
    CodexCallbackInstruction,
    CodexExchangeDeadlines,
    CodexProjectionExpectation,
    CodexProjectionReceipt,
    CodexProjectionReplyLease,
    CodexRefreshRequest,
)
from sidekick_usages.providers.codex.broker.ports import (
    CodexCallbackDispatcher,
    CodexRuntimeStateReader,
    CodexWorkerExchange,
    CodexWorkerExchangeFactory,
)
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime
from sidekick_usages.providers.codex.broker.types import (
    CodexActivationMode,
    CodexBrokerFailure,
    CodexCallbackMode,
)
from sidekick_usages.serialization.framing import clear_mutable_buffer

CODEX_CALLBACK_RESPONSE_SECONDS = 8.0
CODEX_CALLBACK_COMPLETION_SECONDS = 10.0
CODEX_CALLBACK_ERROR_RESERVE_SECONDS = 0.5
CODEX_ACTIVATION_RESPONSE_SECONDS = 90.0
CODEX_ACTIVATION_COMPLETION_SECONDS = 120.0
_BROKER_RECEIVE_SECONDS = 0.25
_BROKER_RECONNECT_INITIAL_SECONDS = 0.25
_BROKER_RECONNECT_MAX_SECONDS = 2.0
_BROKER_JOIN_SECONDS = 3.0
_NANOSECONDS_PER_SECOND = 1_000_000_000


class CodexRuntimeBroker:
    """Own one daemon connection, refresh responder, and activator."""

    def __init__(
        self,
        runtime_factory: Callable[
            [Callable[[], bool]],
            CodexSharedRuntime,
        ],
        runtime_state: CodexRuntimeStateReader,
        callbacks: CodexCallbackDispatcher,
        exchanges: CodexWorkerExchangeFactory,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        status_changed: Callable[[], None] | None = None,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._runtime_state = runtime_state
        self._callbacks = callbacks
        self._exchanges = exchanges
        self._monotonic = monotonic
        self._status_changed = status_changed
        self._stop = Event()
        self._qualified = Event()
        self._ready = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._active_operation: OperationId | None = None
        self._activation_instruction: CodexActivationInstruction | None = None
        self._activation_exchange: CodexWorkerExchange | None = None

    @property
    def ready(self) -> bool:
        """Return whether the selected projection can be refreshed."""
        return self._ready.is_set()

    def prepare_operation(self, operation: DueOperation) -> bool:
        """Prepare an exchange only after resident runtime qualification."""
        if (
            operation.provider_id is not ProviderId.CODEX
            or operation.kind
            not in {OperationKind.ACTIVATE, OperationKind.RECONCILE}
            or operation.priority is not OperationPriority.INTERACTIVE
        ):
            return False
        with self._lock:
            current = self._activation_instruction
            if self._stop.is_set() or not self._qualified.is_set():
                return False
            if current is not None:
                return current.operation_id == operation.operation_id
            prepared = self._prepare_exchange(operation)
        if prepared and self._status_changed is not None:
            self._status_changed()
        return prepared

    def _prepare_exchange(self, operation: DueOperation) -> bool:
        mode = (
            CodexActivationMode.ACTIVATE
            if operation.kind is OperationKind.ACTIVATE
            else CodexActivationMode.RECOVER
        )
        try:
            rollback_account_id = (
                None
                if mode is CodexActivationMode.ACTIVATE
                else self._runtime_state.rollback_account_id(
                    operation.account_id
                )
            )
        except RuntimeError:
            return False
        response_deadline = (
            self._monotonic() + CODEX_ACTIVATION_RESPONSE_SECONDS
        )
        completion_deadline = (
            self._monotonic() + CODEX_ACTIVATION_COMPLETION_SECONDS
        )
        instruction = CodexActivationInstruction(
            operation.operation_id,
            mode,
            operation.account_id,
            rollback_account_id,
            CodexExchangeDeadlines(
                int(response_deadline * _NANOSECONDS_PER_SECOND),
                int(completion_deadline * _NANOSECONDS_PER_SECOND),
            ),
        )
        encoded = encode_codex_activation_instruction(instruction)
        try:
            exchange = self._exchanges.create(
                operation.operation_id,
                encoded,
                response_deadline,
                completion_deadline,
            )
        except RuntimeError:
            return False
        self._activation_instruction = instruction
        self._activation_exchange = exchange
        return True

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
            activation = self._activation_instruction
        self._set_qualified(False)
        self._set_ready(False)
        if operation_id is not None:
            self._callbacks.cancel(operation_id)
        if activation is not None:
            self._exchanges.cancel(activation.operation_id)

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
                    runtime = self._serve_once(runtime)
                    reconnect_seconds = _BROKER_RECONNECT_INITIAL_SECONDS
                except (
                    CodexAppServerError,
                    CodexBrokerError,
                    OSError,
                    RuntimeError,
                    ValueError,
                ) as error:
                    if (
                        isinstance(error, CodexAppServerError)
                        and error.code
                        is CodexAppServerFailure.PROTOCOL_TIMEOUT
                    ):
                        continue
                    self._set_qualified(False)
                    self._set_ready(False)
                    runtime = _drop_runtime(runtime)
                    self._stop.wait(reconnect_seconds)
                    reconnect_seconds = min(
                        reconnect_seconds * 2,
                        _BROKER_RECONNECT_MAX_SECONDS,
                    )
        finally:
            self._set_qualified(False)
            self._set_ready(False)
            _drop_runtime(runtime)

    def _serve_once(
        self,
        runtime: CodexSharedRuntime | None,
    ) -> CodexSharedRuntime | None:
        if runtime is None:
            self._set_qualified(False)
            self._set_ready(False)
            runtime = self._runtime_factory(self._stop.is_set)
            runtime.qualify()
        elif not runtime.qualified:
            self._set_qualified(False)
            self._set_ready(False)
            runtime.qualify()
        self._set_qualified(True)
        activation = self._pending_activation()
        if activation is not None:
            instruction, exchange = activation
            try:
                response_available = exchange.response_available()
            except RuntimeError:
                self._finish_activation(
                    instruction.operation_id,
                    completed=False,
                )
            else:
                if response_available:
                    return self._serve_activation(
                        runtime,
                        instruction,
                        exchange,
                    )
                if exchange.launched:
                    self._set_ready(False)
                    self._stop.wait(_BROKER_RECEIVE_SECONDS)
                    return runtime
        return self._serve_current(runtime)

    def _serve_current(
        self,
        runtime: CodexSharedRuntime,
    ) -> CodexSharedRuntime:
        expectation = self._expectation()
        if expectation is None:
            self._set_ready(True)
            self._stop.wait(_BROKER_RECEIVE_SECONDS)
            return runtime
        receipt = runtime.receipt
        if receipt is None or not receipt.matches(expectation):
            self._set_ready(False)
            receipt = runtime.prepare(
                expectation.account_id,
                expectation.provider_identity,
                expectation.generation,
            )
        if receipt is None:
            receipt = self._rehydrate(runtime, expectation)
        self._set_ready(True)
        self._receive(runtime, expectation, receipt)
        return runtime

    def _serve_activation(
        self,
        runtime: CodexSharedRuntime | None,
        instruction: CodexActivationInstruction,
        exchange: CodexWorkerExchange,
    ) -> CodexSharedRuntime | None:
        if runtime is None:
            self._set_ready(False)
            runtime = self._runtime_factory(self._stop.is_set)
        self._activate(runtime, instruction, exchange)
        return runtime

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

    def _set_qualified(self, qualified: bool) -> None:
        if qualified and self._stop.is_set():
            qualified = False
        current = self._qualified.is_set()
        if current == qualified:
            return
        if qualified:
            self._qualified.set()
        else:
            self._qualified.clear()
        if self._status_changed is not None:
            self._status_changed()

    def _pending_activation(
        self,
    ) -> tuple[CodexActivationInstruction, CodexWorkerExchange] | None:
        with self._lock:
            instruction = self._activation_instruction
            exchange = self._activation_exchange
        if instruction is None or exchange is None:
            return None
        return instruction, exchange

    def _activate(
        self,
        runtime: CodexSharedRuntime,
        instruction: CodexActivationInstruction,
        exchange: CodexWorkerExchange,
    ) -> None:
        completed = False
        self._set_ready(False)
        try:
            payload = exchange.receive_response()
            try:
                reply = decode_codex_activation_reply(
                    payload,
                    instruction,
                )
            finally:
                clear_mutable_buffer(payload)
            with reply:
                runtime.prepare(
                    reply.account_id,
                    reply.provider_identity,
                    reply.generation,
                )
                receipt = runtime.install(
                    reply,
                    deadline=(
                        instruction.deadlines.completion_deadline_seconds
                        - CODEX_CALLBACK_ERROR_RESERVE_SECONDS
                    ),
                )
            exchange.acknowledge(
                encode_codex_activation_acknowledgement(
                    CodexActivationAcknowledgement(
                        instruction.operation_id,
                        instruction.mode,
                        receipt,
                    )
                )
            )
            if not exchange.wait_for_completion():
                raise RuntimeError("Codex activation did not commit.")
            if self._expectation() != CodexProjectionExpectation(
                receipt.account_id,
                receipt.provider_identity,
                receipt.generation,
            ):
                raise RuntimeError("Codex activation state is inconsistent.")
            completed = True
        finally:
            self._finish_activation(
                instruction.operation_id,
                completed=completed,
            )

    def _finish_activation(
        self,
        operation_id: OperationId,
        *,
        completed: bool,
    ) -> None:
        if not completed:
            self._exchanges.cancel(operation_id)
        with self._lock:
            instruction = self._activation_instruction
            if (
                instruction is not None
                and instruction.operation_id == operation_id
            ):
                self._activation_instruction = None
                self._activation_exchange = None
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
            or not receipt.matches(expectation)
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
            if runtime.receipt is None or not runtime.receipt.matches(
                expectation
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
                    instruction.source_generation,
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
    ) -> tuple[CodexCallbackInstruction, CodexWorkerExchange]:
        operation_id = new_operation_id()
        instruction = CodexCallbackInstruction(
            operation_id,
            mode,
            expectation.account_id,
            expectation.provider_identity,
            expectation.generation,
            CodexExchangeDeadlines(
                int(response_deadline * _NANOSECONDS_PER_SECOND),
                int(completion_deadline * _NANOSECONDS_PER_SECOND),
            ),
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
        exchange: CodexWorkerExchange,
        instruction: CodexCallbackInstruction,
    ) -> CodexProjectionReplyLease:
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
        selected = self._runtime_state.current()
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

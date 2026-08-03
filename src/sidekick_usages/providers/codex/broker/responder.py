"""Singleton resident responder for shared-daemon external auth."""

import time
from collections.abc import Callable
from datetime import datetime
from threading import Event, Lock, Thread

from sidekick_usages.core.accounts.identifiers import new_operation_id
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import DueOperation
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.codex.account.types import CodexAuthMode
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.models import (
    JsonRpcNotification,
    JsonRpcServerRequest,
)
from sidekick_usages.providers.codex.app_server.methods import (
    ACCOUNT_CHATGPT_AUTH_REFRESH_METHOD,
    ACCOUNT_UPDATED_METHOD,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.broker.authority import (
    CodexSavedAuthorityResolver,
)
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.external_auth.activation import (
    decode_codex_activation_reply,
    encode_codex_activation_acknowledgement,
    encode_codex_activation_instruction,
)
from sidekick_usages.providers.codex.broker.external_auth.refresh import (
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
from sidekick_usages.providers.codex.broker.native_auth import (
    CodexNativeAuthReconciler,
    CodexNativePreparationGate,
)
from sidekick_usages.providers.codex.broker.ports import (
    CodexOperationDispatcher,
    CodexRuntimeStateReader,
    CodexSavedAccountReader,
    CodexSavedAuthorityRelation,
    CodexWorkerExchange,
    CodexWorkerExchangeFactory,
)
from sidekick_usages.providers.codex.broker.selection import (
    CodexSelectionBroker,
)
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime
from sidekick_usages.providers.codex.broker.types import (
    CodexActivationMode,
    CodexBrokerFailure,
    CodexCallbackMode,
)
from sidekick_usages.providers.codex.session.models import (
    CodexSessionPreparationReport,
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
        saved_accounts: CodexSavedAccountReader,
        operations: CodexOperationDispatcher,
        exchanges: CodexWorkerExchangeFactory,
        *,
        wall_time: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
        status_changed: Callable[[], None] | None = None,
    ) -> None:
        self._runtime_factory = runtime_factory
        self._runtime_state = runtime_state
        self._saved_authority: CodexSavedAuthorityRelation = (
            CodexSavedAuthorityResolver(saved_accounts)
        )
        self._operations = operations
        self._exchanges = exchanges
        self._wall_time = wall_time
        self._monotonic = monotonic
        self._status_changed = status_changed
        self._stop = Event()
        self._qualified = Event()
        self._ready = Event()
        self._lock = Lock()
        self._thread: Thread | None = None
        self._failure_code: str | None = None
        self._preparation_report: CodexSessionPreparationReport | None = None
        self._active_operation: OperationId | None = None
        self._activation_instruction: CodexActivationInstruction | None = None
        self._activation_exchange: CodexWorkerExchange | None = None
        self._selection = CodexSelectionBroker(
            exchanges,
            self._saved_authority,
            runtime_state,
            wall_time=wall_time,
            monotonic=monotonic,
        )
        self._native_auth = CodexNativeAuthReconciler(
            runtime_state,
            self._saved_authority,
            operations,
            wall_time,
            monotonic,
        )
        self._native_preparation = CodexNativePreparationGate(
            self._native_auth,
            monotonic,
        )

    @property
    def ready(self) -> bool:
        """Return whether the selected projection can be refreshed."""
        return self._ready.is_set()

    @property
    def available(self) -> bool:
        """Return whether the official shared runtime is qualified."""
        with self._lock:
            return self._qualified.is_set()

    @property
    def failure_code(self) -> str | None:
        """Return the current safe typed qualification failure."""
        with self._lock:
            return self._failure_code

    @property
    def preparation_report(self) -> CodexSessionPreparationReport | None:
        """Return bounded operator recovery guidance when available."""
        with self._lock:
            return self._preparation_report

    def prepare_operation(self, operation: DueOperation) -> bool:
        """Prepare an exchange only after resident runtime qualification."""
        if operation.kind.is_selection_worker:
            prepared = self._selection.prepare(operation)
            if prepared and self._status_changed is not None:
                self._status_changed()
            return prepared
        if (
            operation.provider_id is ProviderId.CODEX
            and operation.kind is OperationKind.RECONCILE_NATIVE
        ):
            return self._native_preparation.prepare(
                operation,
                stopping=self._stop.is_set,
                qualified=self._qualified.is_set,
            )
        if (
            operation.provider_id is not ProviderId.CODEX
            or operation.kind
            not in {OperationKind.ACTIVATE, OperationKind.RECONCILE}
            or operation.priority is not OperationPriority.INTERACTIVE
            or self._runtime_state.native_reconciliation_pending()
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
                    operation.required_account_id
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
            operation.required_account_id,
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
            self._operations.cancel(operation_id)
        if activation is not None:
            self._exchanges.cancel(activation.operation_id)
        self._selection.cancel()
        self._native_preparation.reset()

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
                    self._record_failure(error)
                    self._set_ready(False)
                    runtime = _drop_runtime(runtime)
                    self._native_auth.reset()
                    self._native_preparation.reset()
                    if _terminal_configuration_failure(error):
                        self._stop.wait()
                    else:
                        self._stop.wait(reconnect_seconds)
                        reconnect_seconds = min(
                            reconnect_seconds * 2,
                            _BROKER_RECONNECT_MAX_SECONDS,
                        )
        finally:
            self._set_qualified(False)
            self._set_ready(False)
            self._native_auth.reset()
            self._native_preparation.reset()
            _drop_runtime(runtime)

    def _serve_once(
        self,
        runtime: CodexSharedRuntime | None,
    ) -> CodexSharedRuntime | None:
        runtime = self._qualified_runtime(runtime)
        self._selection.set_authority(runtime.authority)
        self._set_qualified(True)
        if self._selection.serve_pending(
            runtime,
            self._record_verified_projection,
            self._set_ready,
            self._stop.wait,
            self._status_changed,
            wait_seconds=_BROKER_RECEIVE_SECONDS,
        ):
            return runtime
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
                self._set_ready(False)
                self._stop.wait(_BROKER_RECEIVE_SECONDS)
                return runtime
        if self._native_preparation.observe_requested(runtime):
            self._set_ready(False)
            if self._status_changed is not None:
                self._status_changed()
            return runtime
        if self._native_auth.observe_when_due(
            runtime,
            projection_active=self._expectation() is not None,
        ):
            self._set_ready(False)
        return self._serve_current(runtime)

    def _qualified_runtime(
        self,
        runtime: CodexSharedRuntime | None,
    ) -> CodexSharedRuntime:
        """Return one live qualified runtime, creating it when absent."""
        if runtime is None:
            self._set_qualified(False)
            self._set_ready(False)
            runtime = self._runtime_factory(self._stop.is_set)
            runtime.qualify()
        elif not runtime.qualified:
            self._set_qualified(False)
            self._set_ready(False)
            runtime.qualify()
        return runtime

    def _serve_current(
        self,
        runtime: CodexSharedRuntime,
    ) -> CodexSharedRuntime:
        if self._runtime_state.native_reconciliation_pending():
            self._set_ready(False)
            self._stop.wait(_BROKER_RECEIVE_SECONDS)
            return runtime
        expectation, finalization_pending = self._expectation_state()
        if expectation is None:
            self._set_ready(not finalization_pending)
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
        with self._lock:
            current = self._qualified.is_set()
            current_failure = self._failure_code
            current_report = self._preparation_report
            next_failure = None if qualified else current_failure
            next_report = None if qualified else current_report
            if (
                current == qualified
                and current_failure == next_failure
                and current_report == next_report
            ):
                return
            if qualified:
                self._qualified.set()
            else:
                self._qualified.clear()
            self._failure_code = next_failure
            self._preparation_report = next_report
        if not qualified:
            self._selection.set_authority(None)
        if self._status_changed is not None:
            self._status_changed()

    def _record_failure(self, error: BaseException) -> None:
        failure_code = (
            error.code.value
            if isinstance(error, CodexAppServerError | CodexBrokerError)
            else None
        )
        preparation_report = (
            error.preparation_report
            if isinstance(error, CodexBrokerError)
            else None
        )
        with self._lock:
            changed = (
                self._qualified.is_set()
                or self._failure_code != failure_code
                or self._preparation_report != preparation_report
            )
            self._qualified.clear()
            self._failure_code = failure_code
            self._preparation_report = preparation_report
        self._selection.set_authority(None)
        if changed and self._status_changed is not None:
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
            self._record_verified_projection(runtime, receipt)
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
        try:
            message = runtime.receive(timeout_seconds=_BROKER_RECEIVE_SECONDS)
        except CodexAppServerError as error:
            if error.code is CodexAppServerFailure.PROTOCOL_TIMEOUT:
                return
            raise
        if isinstance(message, JsonRpcNotification):
            self._handle_notification(runtime, message)
            return
        if not isinstance(message, JsonRpcServerRequest):
            return
        if message.method != ACCOUNT_CHATGPT_AUTH_REFRESH_METHOD:
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

    def _handle_notification(
        self,
        runtime: CodexSharedRuntime,
        notification: JsonRpcNotification,
    ) -> None:
        if notification.method != ACCOUNT_UPDATED_METHOD:
            return
        params = notification.params
        if set(params) != {"authMode", "planType"}:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
        auth_mode = params.get("authMode")
        plan = params.get("planType")
        if (auth_mode is not None and not isinstance(auth_mode, str)) or (
            plan is not None and not isinstance(plan, str)
        ):
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
        if auth_mode == CodexAuthMode.CHATGPT_AUTH_TOKENS.value:
            return
        runtime.invalidate_projection()
        self._set_ready(False)
        self._native_auth.observe_change(runtime)

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
            request_id=None,
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
                        operation_id=instruction.operation_id,
                        mode=instruction.mode,
                        generation=reply.generation,
                        selection_epoch=instruction.selection_epoch,
                        request_id=instruction.request_id,
                    )
                )
            )
            if not exchange.wait_for_completion():
                raise RuntimeError("Codex rehydration did not commit.")
            self._record_verified_projection(runtime, receipt)
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
                request_id=request.request_id,
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
                receipt = runtime.respond_refresh(
                    request.request_id,
                    reply,
                    instruction.source_generation,
                    timeout_seconds=remaining,
                )
            responded = True
            exchange.acknowledge(
                encode_codex_callback_acknowledgement(
                    CodexCallbackAcknowledgement(
                        operation_id=instruction.operation_id,
                        mode=instruction.mode,
                        generation=reply.generation,
                        selection_epoch=instruction.selection_epoch,
                        request_id=instruction.request_id,
                    )
                )
            )
            if not exchange.wait_for_completion():
                raise RuntimeError("Codex refresh did not commit.")
            self._record_verified_projection(runtime, receipt)
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
        *,
        request_id: int | None,
    ) -> tuple[CodexCallbackInstruction, CodexWorkerExchange]:
        operation_id = new_operation_id()
        selected = self._selection.callback_selection(expectation)
        instruction = CodexCallbackInstruction(
            operation_id=operation_id,
            mode=mode,
            account_id=expectation.account_id,
            provider_identity=expectation.provider_identity,
            source_generation=expectation.generation,
            selection_epoch=selected.epoch,
            request_id=request_id,
            deadlines=CodexExchangeDeadlines(
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
                exchange = self._operations.dispatch(
                    operation_id,
                    expectation.account_id,
                    request_id,
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

    def _record_verified_projection(
        self,
        runtime: CodexSharedRuntime,
        receipt: CodexProjectionReceipt,
    ) -> None:
        """Persist projection proof only after strong saved-auth relation."""
        observation = runtime.projection_observation(self._wall_time())
        if (
            observation.provider_identity != receipt.provider_identity
            or not self._saved_authority.matches_account(
                receipt.account_id,
                observation,
            )
        ):
            raise RuntimeError("Codex projection identity is inconsistent.")
        self._operations.record_projection(observation)

    def _finish_dispatch(
        self,
        operation_id: OperationId,
        completed: bool,
    ) -> None:
        try:
            if not completed:
                self._operations.cancel(operation_id)
        finally:
            self._clear_active(operation_id)

    def _clear_active(self, operation_id: OperationId) -> None:
        with self._lock:
            if self._active_operation == operation_id:
                self._active_operation = None

    def _expectation(self) -> CodexProjectionExpectation | None:
        expectation, _finalization_pending = self._expectation_state()
        return expectation

    def _expectation_state(
        self,
    ) -> tuple[CodexProjectionExpectation | None, bool]:
        """Resolve readiness without reverting unfinalized provider proof."""
        snapshot = self._runtime_state.current()
        selected = snapshot.finalized_selection
        projection = snapshot.projection_auth
        if snapshot.activation_in_progress:
            return None, True
        if selected is None or selected.provider_id is not ProviderId.CODEX:
            return None, projection is not None
        expectation = self._saved_authority.expectation(selected)
        if expectation is None:
            return None, True
        if (
            projection is not None
            and projection.provider_identity != expectation.provider_identity
        ):
            return None, True
        return expectation, False

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


def _terminal_configuration_failure(error: BaseException) -> bool:
    return (
        isinstance(error, CodexBrokerError)
        and error.code is CodexBrokerFailure.SESSION_CONFIGURATION_REQUIRED
    )

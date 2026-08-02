"""Resident observation of effective native Codex authentication."""

from collections.abc import Callable
from datetime import datetime
from threading import Lock

from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import (
    DueOperation,
    FinalizedSelection,
    ProviderAuthObservation,
)
from sidekick_usages.core.selection.policy import (
    same_provider_auth_authority,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
    ProviderAuthState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.models import CodexDaemonAuthority
from sidekick_usages.providers.codex.broker.ports import (
    CodexOperationDispatcher,
    CodexRuntimeStateReader,
)
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure

_AUTH_OBSERVATION_INTERVAL_SECONDS = 300.0
_NATIVE_PREPARATION_TTL_SECONDS = 1.0


class CodexNativeAuthReconciler:
    """Record native baselines and enqueue only proven account changes."""

    def __init__(
        self,
        runtime_state: CodexRuntimeStateReader,
        operations: CodexOperationDispatcher,
        wall_time: Callable[[], datetime],
        monotonic: Callable[[], float],
    ) -> None:
        self._runtime_state = runtime_state
        self._operations = operations
        self._wall_time = wall_time
        self._monotonic = monotonic
        self._observed_authority: CodexDaemonAuthority | None = None
        self._next_observation_at = 0.0

    def reset(self) -> None:
        """Forget connection-local observation scheduling state."""
        self._observed_authority = None
        self._next_observation_at = 0.0

    def observe_when_due(
        self,
        runtime: CodexSharedRuntime,
        *,
        projection_active: bool,
    ) -> bool:
        """Observe startup or an unprojected runtime when its bound is due."""
        authority = _authority(runtime)
        if (
            authority == self._observed_authority
            and self._monotonic() < self._next_observation_at
        ):
            return False
        if authority == self._observed_authority and projection_active:
            self._next_observation_at = self._next_deadline()
            return False
        priority = (
            OperationPriority.INTERACTIVE
            if authority != self._observed_authority
            else OperationPriority.SCHEDULED
        )
        return self._observe(runtime, priority, force=False) is not None

    def observe_change(
        self,
        runtime: CodexSharedRuntime,
    ) -> DueOperation:
        """Record and urgently reconcile one official account-change signal."""
        operation = self._observe(
            runtime,
            OperationPriority.INTERACTIVE,
            force=True,
        )
        if operation is None:
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_CHANGED)
        return operation

    def observe_for_launch(self, runtime: CodexSharedRuntime) -> None:
        """Record fresh auth for one already-durable native launch."""
        authority = _authority(runtime)
        observation = runtime.observe_auth(self._wall_time())
        self._operations.record_native(observation)
        self._observed_authority = authority
        self._next_observation_at = self._next_deadline()

    def _observe(
        self,
        runtime: CodexSharedRuntime,
        priority: OperationPriority,
        *,
        force: bool,
    ) -> DueOperation | None:
        authority = _authority(runtime)
        observation = runtime.observe_auth(self._wall_time())
        operation = self._record_observation(
            observation,
            priority,
            force=force,
        )
        self._observed_authority = authority
        self._next_observation_at = (
            0.0
            if operation is not None
            and operation.state is OperationState.RUNNING
            else self._next_deadline()
        )
        return operation

    def _record_observation(
        self,
        observation: ProviderAuthObservation,
        priority: OperationPriority,
        *,
        force: bool,
    ) -> DueOperation | None:
        baseline = self._runtime_state.native_auth_baseline()
        if baseline is None:
            baseline = self._operations.native_observation()
        snapshot = self._runtime_state.current()
        projection = snapshot.projection_auth
        selected = (
            None
            if snapshot.activation_in_progress
            else snapshot.finalized_selection
        )
        retained_projection = (
            not force
            and projection is not None
            and same_provider_auth_authority(observation, projection)
            and _selected_matches_projection(selected, projection)
        )
        if retained_projection:
            return None
        changed = baseline is not None and not same_provider_auth_authority(
            observation, baseline
        )
        uninitialized = baseline is None and not _selected_matches_observation(
            selected, observation
        )
        if force or changed or uninitialized:
            return self._operations.reconcile_native(observation, priority)
        self._operations.record_native(observation)
        return None

    def _next_deadline(self) -> float:
        return self._monotonic() + _AUTH_OBSERVATION_INTERVAL_SECONDS


class CodexNativePreparationGate:
    """Bind each native worker launch to fresh effective runtime proof."""

    def __init__(
        self,
        reconciler: CodexNativeAuthReconciler,
        monotonic: Callable[[], float],
    ) -> None:
        self._reconciler = reconciler
        self._monotonic = monotonic
        self._lock = Lock()
        self._requested: OperationId | None = None
        self._prepared: tuple[OperationId, float] | None = None

    def prepare(
        self,
        operation: DueOperation,
        *,
        stopping: Callable[[], bool],
        qualified: Callable[[], bool],
    ) -> bool:
        """Consume only fresh proof or request a resident observation."""
        if (
            operation.provider_id is not ProviderId.CODEX
            or operation.kind is not OperationKind.RECONCILE_NATIVE
            or operation.priority
            not in {
                OperationPriority.INTERACTIVE,
                OperationPriority.SCHEDULED,
            }
            or operation.account_id is not None
        ):
            return False
        with self._lock:
            prepared = self._prepared
            if (
                prepared is not None
                and prepared[0] == operation.operation_id
                and self._monotonic() <= prepared[1]
            ):
                self._prepared = None
                return True
            self._prepared = None
            if stopping() or not qualified():
                return False
            self._requested = operation.operation_id
        return False

    def observe_requested(
        self,
        runtime: CodexSharedRuntime,
    ) -> bool:
        """Observe and prepare one still-requested native operation."""
        with self._lock:
            operation_id = self._requested
        if operation_id is None:
            return False
        self._reconciler.observe_for_launch(runtime)
        with self._lock:
            if self._requested != operation_id:
                return False
            self._requested = None
            self._prepared = (
                operation_id,
                self._monotonic() + _NATIVE_PREPARATION_TTL_SECONDS,
            )
        return True

    def reset(self) -> None:
        """Invalidate proof when its owning daemon runtime changes."""
        with self._lock:
            self._requested = None
            self._prepared = None


def _authority(runtime: CodexSharedRuntime) -> CodexDaemonAuthority:
    authority = runtime.authority
    if authority is None:
        raise CodexBrokerError(CodexBrokerFailure.RUNTIME_CHANGED)
    return authority


def _selected_matches_observation(
    selected: FinalizedSelection | None,
    observation: ProviderAuthObservation,
) -> bool:
    return (
        selected is not None
        and selected.provider_id is observation.provider_id
        and observation.state is ProviderAuthState.ACTIVE
        and selected.generation == observation.generation
    )


def _selected_matches_projection(
    selected: FinalizedSelection | None,
    projection: ProviderAuthObservation,
) -> bool:
    return (
        selected is not None
        and selected.provider_id is projection.provider_id
        and projection.state is ProviderAuthState.ACTIVE
        and selected.generation == projection.generation
    )

"""Resident observation of effective native Codex authentication."""

from collections.abc import Callable
from datetime import datetime

from sidekick_usages.core.selection.models import (
    DueOperation,
    ProviderAuthObservation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.policy import (
    same_provider_auth_authority,
)
from sidekick_usages.core.selection.types import (
    OperationPriority,
    OperationState,
    ProviderAuthState,
    ProviderRuntimeState,
)
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.models import CodexDaemonAuthority
from sidekick_usages.providers.codex.broker.ports import (
    CodexOperationDispatcher,
    CodexRuntimeStateReader,
)
from sidekick_usages.providers.codex.broker.service import CodexSharedRuntime
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure

_AUTH_OBSERVATION_INTERVAL_SECONDS = 300.0


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
        return self._observe(runtime, priority, force=False)

    def observe_change(self, runtime: CodexSharedRuntime) -> None:
        """Record and urgently reconcile one official account-change signal."""
        self._observe(
            runtime,
            OperationPriority.INTERACTIVE,
            force=True,
        )

    def _observe(
        self,
        runtime: CodexSharedRuntime,
        priority: OperationPriority,
        *,
        force: bool,
    ) -> bool:
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
        return operation is not None

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
        projection = self._operations.projection_observation()
        selected = self._runtime_state.current()
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


def _authority(runtime: CodexSharedRuntime) -> CodexDaemonAuthority:
    authority = runtime.authority
    if authority is None:
        raise CodexBrokerError(CodexBrokerFailure.RUNTIME_CHANGED)
    return authority


def _selected_matches_observation(
    selected: SelectedAccountState | None,
    observation: ProviderAuthObservation,
) -> bool:
    if selected is None or selected.provider_id is not observation.provider_id:
        return False
    if selected.runtime_state is ProviderRuntimeState.SAVED_ACTIVE:
        return True
    if observation.state is ProviderAuthState.ACTIVE:
        return (
            selected.runtime_state is ProviderRuntimeState.EXTERNAL_ACTIVE
            and selected.provider_identity == observation.provider_identity
            and selected.runtime_generation == observation.generation
        )
    expected = {
        ProviderAuthState.LOGGED_OUT: ProviderRuntimeState.LOGGED_OUT,
        ProviderAuthState.UNREADABLE: ProviderRuntimeState.UNREADABLE,
        ProviderAuthState.UNSUPPORTED: ProviderRuntimeState.UNSUPPORTED,
    }[observation.state]
    return selected.runtime_state is expected


def _selected_matches_projection(
    selected: SelectedAccountState | None,
    projection: ProviderAuthObservation,
) -> bool:
    return (
        selected is not None
        and selected.provider_id is projection.provider_id
        and selected.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
        and selected.provider_identity == projection.provider_identity
    )

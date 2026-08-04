"""Isolated verified Claude selection execution."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import AuthorityGeneration
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    DueOperation,
    FinalizedSelection,
    OpenSelectionOperation,
    PreparedSelection,
    SelectedAccountState,
    SelectionAuthorityObservation,
)
from sidekick_usages.core.selection.policy import protected_selection_enabled
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    ProviderRuntimeState,
    SelectionCode,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationError,
    ClaudeActivationFailure,
)
from sidekick_usages.credentials.claude.activation.reconciliation import (
    ClaudeNativeReconciliationService,
)
from sidekick_usages.credentials.claude.activation.recovery import (
    ClaudeActivationRecoveryService,
)
from sidekick_usages.credentials.claude.activation.service import (
    ClaudeActivationService,
)
from sidekick_usages.credentials.claude.authority.access_lease import (
    ClaudeAccessLease,
    ClaudeAuthorityMode,
    ClaudePreparedAuthority,
    ClaudeSelectedAccessError,
    ClaudeSelectedAccessLeaseService,
)
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.daemon.worker.runtime import (
    managed_worker_result,
    selection_worker_success,
    worker_no_change,
    worker_success,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)
from sidekick_usages.providers.claude.activation.types import (
    ClaudeActivationGuardFailure,
)
from sidekick_usages.providers.claude.structured.codec import (
    clear_secret_buffer,
)
from sidekick_usages.providers.claude.structured.data_plane import (
    ClaudeProtectedProjectionWriter,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredBinding,
)

_CLAUDE_SELECTION_KINDS = frozenset(
    {
        OperationKind.ACTIVATE,
        OperationKind.RECONCILE,
        OperationKind.RECONCILE_NATIVE,
    }
)
_CLAUDE_SELECTION_WORKER_KINDS = frozenset(
    {
        OperationKind.SELECTION_PREVALIDATE,
        OperationKind.SELECTION_COMMIT,
        OperationKind.SELECTION_READBACK,
        OperationKind.CLAUDE_PARTICIPANT_BIND,
    }
)

_CLAUDE_ACTIVATION_SELECTION_FAILURES = {
    ClaudeActivationFailure.INCOMPATIBLE: (
        WorkerOutcome.UNSUPPORTED,
        SelectionCode.UNSUPPORTED_PROVIDER_VERSION,
    ),
    ClaudeActivationFailure.NATIVE_CHANGED: (
        WorkerOutcome.ACTION_REQUIRED,
        SelectionCode.UNCOORDINATED_AUTH_MUTATION,
    ),
    ClaudeActivationFailure.NATIVE_UNAVAILABLE: (
        WorkerOutcome.TRANSIENT_FAILURE,
        SelectionCode.PROVIDER_UNAVAILABLE,
    ),
    ClaudeActivationFailure.RECONCILIATION_REQUIRED: (
        WorkerOutcome.ACTION_REQUIRED,
        SelectionCode.UNCOORDINATED_AUTH_MUTATION,
    ),
    ClaudeActivationFailure.SOURCE_UNAVAILABLE: (
        WorkerOutcome.ACTION_REQUIRED,
        SelectionCode.AUTHORITY_PROOF_FAILED,
    ),
    ClaudeActivationFailure.STATE_CHANGED: (
        WorkerOutcome.TRANSIENT_FAILURE,
        SelectionCode.AUTHORITY_PROOF_FAILED,
    ),
    ClaudeActivationFailure.TARGET_UNAVAILABLE: (
        WorkerOutcome.ACTION_REQUIRED,
        SelectionCode.TARGET_REFRESH_REQUIRED,
    ),
    ClaudeActivationFailure.TIMED_OUT: (
        WorkerOutcome.TIMED_OUT,
        SelectionCode.ACTIVE_OPERATION_TIMEOUT,
    ),
}


def claude_selection_failure(
    operation: DueOperation,
    error: ClaudeActivationError,
    clock: Clock,
    *,
    recovery_required: bool = False,
) -> WorkerResult:
    """Map every Claude refusal into one closed selection worker result."""
    if recovery_required:
        outcome = WorkerOutcome.TRANSIENT_FAILURE
        code = SelectionCode.SELECTION_RECOVERY_REQUIRED
    elif isinstance(error.failure, ClaudeActivationGuardFailure):
        outcome = WorkerOutcome.ACTION_REQUIRED
        code = (
            SelectionCode.REMOTE_CONTROL_STATE_INCOMPATIBLE
            if error.failure
            is ClaudeActivationGuardFailure.REMOTE_CONTROL_INCOMPATIBLE
            else SelectionCode.SESSION_CONFIGURATION_REQUIRED
        )
    else:
        outcome, code = _CLAUDE_ACTIVATION_SELECTION_FAILURES[error.failure]
    return WorkerResult(
        operation_id=operation.operation_id,
        outcome=outcome,
        finished_at=clock.now(),
        failure_code=code.value,
    )


class ClaudeSelectionWorkerExecutor:
    """Run Claude selection and native reads without credential exchange."""

    def __init__(
        self,
        activation: ClaudeActivationService,
        recovery: ClaudeActivationRecoveryService,
        native_reconciliation: ClaudeNativeReconciliationService,
        clock: Clock,
        access: ClaudeSelectedAccessLeaseService | None = None,
        projection: ClaudeProtectedProjectionWriter | None = None,
    ) -> None:
        self._activation = activation
        self._recovery = recovery
        self._native_reconciliation = native_reconciliation
        self._clock = clock
        self._access = access
        self._projection = projection

    def execute(
        self,
        operation: DueOperation,
        authority: ProviderMutationAuthority,
    ) -> WorkerResult:
        """Execute one admitted Claude selection transaction."""
        authority.require(ProviderId.CLAUDE)
        supported_priority = (
            operation.priority is OperationPriority.INTERACTIVE
            or (
                operation.kind is OperationKind.RECONCILE_NATIVE
                and operation.priority is OperationPriority.SCHEDULED
            )
        )
        if (
            operation.provider_id is not ProviderId.CLAUDE
            or operation.kind not in _CLAUDE_SELECTION_KINDS
            or not supported_priority
        ):
            raise ValueError("Worker operation is not Claude selection.")
        related = None
        try:
            if operation.kind is OperationKind.ACTIVATE:
                self._activation.activate(
                    operation.operation_id,
                    operation.required_account_id,
                    authority,
                )
            elif operation.kind is OperationKind.RECONCILE:
                self._recovery.recover(
                    operation.required_account_id,
                    authority,
                )
            else:
                reconciled = self._native_reconciliation.reconcile(authority)
                related = reconciled.related_runtime_authority
                if not reconciled.changed:
                    return worker_no_change(
                        operation,
                        self._clock,
                        reconciled.related_runtime_authority,
                    )
        except ClaudeActivationError as error:
            return managed_worker_result(
                operation,
                self._clock,
                succeeded=False,
                action_required=(
                    error.action_required
                    and not (
                        operation.kind is OperationKind.RECONCILE_NATIVE
                        and error.failure
                        is ClaudeActivationFailure.NATIVE_CHANGED
                    )
                ),
                timed_out=error.timed_out,
                failure_code=error.failure_code,
            )
        return worker_success(
            operation,
            self._clock,
            related,
        )

    def execute_selection(
        self,
        operation: DueOperation,
        active: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
        authority: ProviderMutationAuthority,
    ) -> WorkerResult:
        """Execute one exact Claude global-selection worker phase."""
        authority.require(ProviderId.CLAUDE)
        self._require_selection_work(operation, active)
        if active.baseline_account_id == active.target_account_id:
            return self._execute_generation_rollover(
                operation,
                active,
                baseline,
                authority,
            )
        try:
            if operation.kind is OperationKind.SELECTION_PREVALIDATE:
                prepared = self.prevalidate_selection(
                    active,
                    baseline,
                    authority,
                )
                observation = SelectionAuthorityObservation(
                    provider_id=ProviderId.CLAUDE,
                    account_id=prepared.target_account_id,
                    generation=prepared.target_generation,
                )
            elif operation.kind is OperationKind.SELECTION_COMMIT:
                generation = active.prepared_generation
                if generation is None:
                    raise ClaudeActivationError(
                        ClaudeActivationFailure.STATE_CHANGED
                    )
                prepared = PreparedSelection(
                    operation_id=active.operation_id,
                    provider_id=active.provider_id,
                    target_account_id=active.target_account_id,
                    target_generation=generation,
                    baseline_epoch=active.baseline_epoch,
                    pending_epoch=active.pending_epoch,
                )
                proof = self.commit_selection(prepared, authority)
                observation = SelectionAuthorityObservation(
                    provider_id=ProviderId.CLAUDE,
                    account_id=proof.account_id,
                    generation=proof.generation,
                )
            elif operation.kind is OperationKind.CLAUDE_PARTICIPANT_BIND:
                generation = active.target_generation
                if generation is None:
                    raise ClaudeSelectedAccessError(
                        "The protected Claude target is unproven."
                    )
                self.bind_selection(
                    active,
                    generation,
                    authority,
                )
                observation = SelectionAuthorityObservation(
                    provider_id=ProviderId.CLAUDE,
                    account_id=active.target_account_id,
                    generation=generation,
                )
            else:
                observation = self.recovery_readback_selection(
                    active, authority
                )
        except ClaudeActivationError as error:
            return claude_selection_failure(
                operation,
                error,
                self._clock,
                recovery_required=(
                    operation.kind is OperationKind.SELECTION_READBACK
                ),
            )
        except ClaudeSelectedAccessError as error:
            return WorkerResult(
                operation_id=operation.operation_id,
                outcome=WorkerOutcome.ACTION_REQUIRED,
                finished_at=self._clock.now(),
                failure_code=error.code.value,
            )
        return selection_worker_success(
            operation,
            active.pending_epoch,
            observation,
            self._clock,
        )

    def _execute_generation_rollover(
        self,
        operation: DueOperation,
        active: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
        authority: ProviderMutationAuthority,
    ) -> WorkerResult:
        """Execute one provider-observed same-account generation rollover."""
        try:
            target, generation = self._generation_rollover_target(
                active,
                baseline,
                authority,
            )
            expected = (
                active.prepared_generation
                if operation.kind is OperationKind.SELECTION_COMMIT
                else active.target_generation
                if operation.kind is OperationKind.CLAUDE_PARTICIPANT_BIND
                else None
            )
            if operation.kind is not OperationKind.SELECTION_READBACK and (
                baseline is None
                or generation == baseline.generation
                or (expected is not None and generation != expected)
            ):
                raise ClaudeActivationError(
                    ClaudeActivationFailure.STATE_CHANGED
                )
            if operation.kind in {
                OperationKind.SELECTION_COMMIT,
                OperationKind.CLAUDE_PARTICIPANT_BIND,
            }:
                if expected is None:
                    raise ClaudeActivationError(
                        ClaudeActivationFailure.STATE_CHANGED
                    )
                access = self._access
                if access is None:
                    raise ClaudeSelectedAccessError(
                        "The selected Claude access lease is unavailable."
                    )
                with access.open_rollover_proven(
                    target,
                    generation,
                    authority,
                ) as lease:
                    self._project(
                        ClaudeStructuredBinding(
                            operation_id=active.operation_id,
                            account_id=active.target_account_id,
                            generation=generation,
                            epoch=active.pending_epoch,
                        ),
                        lease,
                    )
        except ClaudeActivationError as error:
            return claude_selection_failure(
                operation,
                error,
                self._clock,
                recovery_required=(
                    operation.kind is OperationKind.SELECTION_READBACK
                ),
            )
        except ClaudeSelectedAccessError as error:
            return WorkerResult(
                operation_id=operation.operation_id,
                outcome=WorkerOutcome.ACTION_REQUIRED,
                finished_at=self._clock.now(),
                failure_code=error.code.value,
            )
        return selection_worker_success(
            operation,
            active.pending_epoch,
            SelectionAuthorityObservation(
                provider_id=ProviderId.CLAUDE,
                account_id=active.target_account_id,
                generation=generation,
                authority_requires_participant=(
                    False
                    if operation.kind is OperationKind.SELECTION_READBACK
                    else None
                ),
            ),
            self._clock,
        )

    @staticmethod
    def _require_selection_work(
        operation: DueOperation,
        active: OpenSelectionOperation,
    ) -> None:
        """Require one exact interactive Claude selection child."""
        if (
            operation.provider_id is not ProviderId.CLAUDE
            or operation.kind not in _CLAUDE_SELECTION_WORKER_KINDS
            or operation.priority is not OperationPriority.INTERACTIVE
            or active.provider_id is not ProviderId.CLAUDE
            or operation.required_selection_operation_id != active.operation_id
            or operation.required_account_id != active.target_account_id
        ):
            raise ValueError("Worker operation is not Claude selection work.")

    def _generation_rollover_target(
        self,
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
        authority: ProviderMutationAuthority,
    ) -> tuple[ClaudePreparedAuthority, AuthorityGeneration]:
        """Require one refreshable target and stable newer native proof."""
        if (
            baseline is None
            or baseline.provider_id is not ProviderId.CLAUDE
            or baseline.account_id != operation.target_account_id
            or baseline.account_id != operation.baseline_account_id
            or baseline.epoch != operation.baseline_epoch
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        access = self._access
        if access is None:
            raise ClaudeSelectedAccessError(
                "The selected Claude access lease is unavailable."
            )
        target = access.prevalidate(operation.target_account_id, authority)
        if target.mode is not ClaudeAuthorityMode.REFRESHABLE:
            raise ClaudeSelectedAccessError(
                "Claude generation rollover requires refreshable authority."
            )
        selected = self._native_reconciliation.observe_selection(
            (operation.target_account_id,),
            authority,
        )
        generation = None if selected is None else selected.runtime_generation
        if (
            selected is None
            or selected.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
            or selected.account_id != operation.target_account_id
            or generation is None
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)
        return target, generation

    def execute_finalized_bind(
        self,
        operation: DueOperation,
        finalized: FinalizedSelection,
        authority: ProviderMutationAuthority,
    ) -> WorkerResult:
        """Bind finalized Claude authority without a selection journal."""
        authority.require(ProviderId.CLAUDE)
        if (
            operation.provider_id is not ProviderId.CLAUDE
            or operation.kind is not OperationKind.CLAUDE_PARTICIPANT_BIND
            or operation.priority is not OperationPriority.INTERACTIVE
            or operation.required_selection_operation_id
            != operation.operation_id
            or finalized.provider_id is not ProviderId.CLAUDE
            or operation.required_account_id != finalized.account_id
        ):
            raise ValueError(
                "Worker operation is not a finalized Claude bind."
            )
        try:
            self._bind_target(
                ClaudeStructuredBinding(
                    operation_id=operation.operation_id,
                    account_id=finalized.account_id,
                    generation=finalized.generation,
                    epoch=finalized.epoch,
                ),
                authority,
            )
        except ClaudeSelectedAccessError:
            return WorkerResult(
                operation_id=operation.operation_id,
                outcome=WorkerOutcome.ACTION_REQUIRED,
                finished_at=self._clock.now(),
                failure_code=SelectionCode.AUTHORITY_PROOF_FAILED.value,
            )
        return selection_worker_success(
            operation,
            finalized.epoch,
            SelectionAuthorityObservation(
                provider_id=ProviderId.CLAUDE,
                account_id=finalized.account_id,
                generation=finalized.generation,
            ),
            self._clock,
        )

    def prevalidate_selection(
        self,
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
        authority: ProviderMutationAuthority,
    ) -> PreparedSelection:
        """Prove one target before participant admission closes."""
        self._require_open_operation(operation, baseline)
        access = self._access
        if access is None:
            generation = self._activation.prevalidate(
                operation.target_account_id,
                authority,
            )
        else:
            target = access.prevalidate(
                operation.target_account_id,
                authority,
            )
            generation = target.generation
        if baseline is None:
            native = self._native_reconciliation.observe_selection(
                (operation.target_account_id,),
                authority,
            )
            admissible = native is not None and (
                native.runtime_state is ProviderRuntimeState.LOGGED_OUT
                or (
                    native.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
                    and native.account_id == operation.target_account_id
                )
            )
            if not admissible:
                raise ClaudeActivationError(
                    ClaudeActivationFailure.RECONCILIATION_REQUIRED
                )
        return PreparedSelection(
            operation_id=operation.operation_id,
            provider_id=ProviderId.CLAUDE,
            target_account_id=operation.target_account_id,
            target_generation=generation,
            baseline_epoch=operation.baseline_epoch,
            pending_epoch=operation.pending_epoch,
        )

    def commit_selection(
        self,
        prepared: PreparedSelection,
        authority: ProviderMutationAuthority,
    ) -> AuthorityReadyProof:
        """Run the official activation and return its runtime generation."""
        self._require_prepared(prepared)
        if self._access is not None:
            target = self._access.prevalidate(
                prepared.target_account_id,
                authority,
            )
            if target.generation != prepared.target_generation:
                raise ClaudeSelectedAccessError(
                    "The selected Claude generation changed."
                )
            if (
                target.mode is ClaudeAuthorityMode.REFRESHABLE
                and not protected_selection_enabled(ProviderId.CLAUDE)
            ):
                selected = self._activation.activate(
                    prepared.operation_id,
                    prepared.target_account_id,
                    authority,
                    expected_target_generation=prepared.target_generation,
                )
                if selected.runtime_generation is None:
                    raise ClaudeSelectedAccessError(
                        "The selected Claude generation is unavailable."
                    )
                return self._proof(prepared, selected.runtime_generation)
            if not protected_selection_enabled(ProviderId.CLAUDE):
                raise ClaudeSelectedAccessError(
                    "Protected Claude selection remains disabled.",
                    SelectionCode.UNSUPPORTED_SESSION_CAPABILITY,
                )
            with self._access.open_committed(
                prepared.operation_id,
                target,
                authority,
            ) as access:
                committed_generation = access.prepared.generation
                binding = ClaudeStructuredBinding(
                    operation_id=prepared.operation_id,
                    account_id=prepared.target_account_id,
                    generation=committed_generation,
                    epoch=prepared.pending_epoch,
                )
                if self._projection is not None:
                    self._project(binding, access)
                elif target.mode is ClaudeAuthorityMode.SETUP:
                    raise ClaudeSelectedAccessError(
                        "The protected Claude projection is unavailable."
                    )
            return self._proof(prepared, committed_generation)
        selected = self._activation.activate(
            prepared.operation_id,
            prepared.target_account_id,
            authority,
            expected_target_generation=prepared.target_generation,
        )
        if (
            selected.provider_id is not ProviderId.CLAUDE
            or selected.account_id != prepared.target_account_id
            or selected.runtime_generation is None
        ):
            raise ClaudeActivationError(
                ClaudeActivationFailure.RECONCILIATION_REQUIRED
            )
        return self._proof(prepared, selected.runtime_generation)

    def readback_selection(
        self,
        operation: OpenSelectionOperation,
        authority: ProviderMutationAuthority,
    ) -> SelectedAccountState | None:
        """Observe native truth against one open journal context."""
        self._require_readback_operation(operation)
        account_ids = (
            (operation.target_account_id,)
            if operation.baseline_account_id is None
            else (
                operation.baseline_account_id,
                operation.target_account_id,
            )
        )
        return self._native_reconciliation.observe_selection(
            account_ids,
            authority,
        )

    def recovery_readback_selection(
        self,
        operation: OpenSelectionOperation,
        authority: ProviderMutationAuthority,
    ) -> SelectionAuthorityObservation:
        """Reclassify the target before returning native recovery truth."""
        self._require_readback_operation(operation)
        expected_generation = operation.prepared_generation
        access = self._access
        if access is None or expected_generation is None:
            raise ClaudeSelectedAccessError(
                "The selected Claude recovery target is unavailable."
            )
        target = access.prevalidate(operation.target_account_id, authority)
        if target.generation != expected_generation:
            raise ClaudeSelectedAccessError(
                "The selected Claude recovery target changed."
            )
        selected = self.readback_selection(operation, authority)
        observed = self._selection_observation(selected)
        return SelectionAuthorityObservation(
            provider_id=observed.provider_id,
            account_id=observed.account_id,
            generation=observed.generation,
            authority_requires_participant=(
                target.mode is ClaudeAuthorityMode.SETUP
            ),
        )

    def bind_selection(
        self,
        operation: OpenSelectionOperation,
        generation: AuthorityGeneration,
        authority: ProviderMutationAuthority,
    ) -> None:
        """Project one already-proven target without native mutation."""
        self._bind_target(
            ClaudeStructuredBinding(
                operation_id=operation.operation_id,
                account_id=operation.target_account_id,
                generation=generation,
                epoch=operation.pending_epoch,
            ),
            authority,
        )

    def _bind_target(
        self,
        binding: ClaudeStructuredBinding,
        authority: ProviderMutationAuthority,
    ) -> None:
        access = self._access
        if access is None:
            raise ClaudeSelectedAccessError(
                "The protected Claude bind is unavailable."
            )
        target = access.prevalidate(binding.account_id, authority)
        with access.open_proven(
            target,
            binding.generation,
            authority,
        ) as lease:
            self._project(binding, lease)

    def _project(
        self,
        binding: ClaudeStructuredBinding,
        lease: ClaudeAccessLease,
    ) -> None:
        projection = self._projection
        if projection is None:
            raise ClaudeSelectedAccessError(
                "The protected Claude projection is unavailable."
            )
        oauth = lease.oauth_buffer()
        try:
            projection.submit(binding, oauth)
        finally:
            clear_secret_buffer(oauth)

    @staticmethod
    def _selection_observation(
        selected: SelectedAccountState | None,
    ) -> SelectionAuthorityObservation:
        """Sanitize native readback to one related saved authority or none."""
        if (
            selected is None
            or selected.account_id is None
            or selected.runtime_generation is None
        ):
            return SelectionAuthorityObservation(
                provider_id=ProviderId.CLAUDE,
                account_id=None,
                generation=None,
            )
        return SelectionAuthorityObservation(
            provider_id=ProviderId.CLAUDE,
            account_id=selected.account_id,
            generation=selected.runtime_generation,
        )

    @staticmethod
    def _require_open_operation(
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
    ) -> None:
        valid_unselected = (
            baseline is None
            and operation.baseline_account_id is None
            and operation.baseline_epoch.value == 0
        )
        valid_selected = (
            baseline is not None
            and baseline.provider_id is ProviderId.CLAUDE
            and operation.baseline_account_id == baseline.account_id
            and operation.baseline_epoch == baseline.epoch
            and operation.target_account_id != baseline.account_id
        )
        if operation.provider_id is not ProviderId.CLAUDE or not (
            valid_unselected or valid_selected
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)

    @staticmethod
    def _require_prepared(prepared: PreparedSelection) -> None:
        if prepared.provider_id is not ProviderId.CLAUDE:
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)

    @staticmethod
    def _require_readback_operation(
        operation: OpenSelectionOperation,
    ) -> None:
        valid_baseline = (
            operation.baseline_account_id is None
            and operation.baseline_epoch.value == 0
        ) or (
            operation.baseline_account_id is not None
            and operation.baseline_account_id != operation.target_account_id
        )
        if (
            operation.provider_id is not ProviderId.CLAUDE
            or not valid_baseline
        ):
            raise ClaudeActivationError(ClaudeActivationFailure.STATE_CHANGED)

    @staticmethod
    def _proof(
        prepared: PreparedSelection,
        generation: AuthorityGeneration,
    ) -> AuthorityReadyProof:
        return AuthorityReadyProof(
            provider_id=ProviderId.CLAUDE,
            account_id=prepared.target_account_id,
            generation=generation,
            epoch=prepared.pending_epoch,
            safe_code=SelectionCode.SELECTION_SUCCEEDED,
        )

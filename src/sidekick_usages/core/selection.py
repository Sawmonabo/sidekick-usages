"""Pure provider-selection, activation, and due-operation policy."""

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from sidekick_usages.core.accounts import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import ProviderId

_MAX_ATTEMPTS = 1_000_000
_MAX_SAFE_CODE_BYTES = 128


class ProviderRuntimeState(StrEnum):
    """Closed provider-native authentication observations."""

    SAVED_ACTIVE = "saved_active"
    EXTERNAL_ACTIVE = "external_active"
    LOGGED_OUT = "logged_out"
    UNREADABLE = "unreadable"
    UNSUPPORTED = "unsupported"


class ActivationPhase(StrEnum):
    """Closed durable phases for one provider activation transaction."""

    PREPARED = "prepared"
    OUTGOING_RETAINED = "outgoing_retained"
    TARGET_ACTIVATED = "target_activated"
    READ_BACK_VERIFIED = "read_back_verified"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    RECONCILIATION_REQUIRED = "reconciliation_required"

    @property
    def terminal(self) -> bool:
        """Return whether no later activation phase is legal."""
        return self in {
            ActivationPhase.COMMITTED,
            ActivationPhase.ROLLED_BACK,
            ActivationPhase.RECONCILIATION_REQUIRED,
        }


class ActivationOutcome(StrEnum):
    """Sanitized result of provider read-back or activation recovery."""

    VERIFIED = "verified"
    ROLLED_BACK = "rolled_back"
    EXTERNAL_RECONCILED = "external_reconciled"
    LOGGED_OUT = "logged_out"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    UNSUPPORTED = "unsupported"


class ActivationRecoveryAction(StrEnum):
    """Required recovery action selected from actual provider read-back."""

    COMMIT_VERIFIED = "commit_verified"
    ROLLBACK_VERIFIED = "rollback_verified"
    RECONCILE_EXTERNAL = "reconcile_external"
    REQUEST_OFFICIAL_ROLLBACK = "request_official_rollback"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    CLOSE_FAILED = "close_failed"


class OperationKind(StrEnum):
    """Closed worker operation kinds."""

    MAINTAIN = "maintain"
    REFRESH = "refresh"
    USAGE = "usage"
    ACTIVITY = "activity"
    LOGIN = "login"
    MIGRATE = "migrate"
    ACTIVATE = "activate"
    REPAIR = "repair"
    RECONCILE = "reconcile"


class OperationPriority(StrEnum):
    """Closed scheduling lanes in descending urgency."""

    CODEX_CALLBACK = "codex_callback"
    INTERACTIVE = "interactive"
    SCHEDULED = "scheduled"

    @property
    def rank(self) -> int:
        """Return a lower numeric rank for more urgent work."""
        return (
            OperationPriority.CODEX_CALLBACK,
            OperationPriority.INTERACTIVE,
            OperationPriority.SCHEDULED,
        ).index(self)


class OperationState(StrEnum):
    """Closed lifecycle for one durable account-operation slot."""

    SCHEDULED = "scheduled"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    ACTION_REQUIRED = "action_required"


def _safe_code(value: str | None) -> str | None:
    """Validate one bounded non-secret machine-readable outcome code."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("Safe outcome code must be text.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Safe outcome code must be valid UTF-8.") from None
    if (
        not encoded
        or len(encoded) > _MAX_SAFE_CODE_BYTES
        or not all(
            character.isascii()
            and (
                character.islower() or character.isdigit() or character == "_"
            )
            for character in value
        )
    ):
        raise ValueError(
            "Safe outcome code must use bounded lowercase ASCII identifiers."
        )
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectedAccountState:
    """Last provider-verified runtime authentication state."""

    provider_id: ProviderId
    runtime_state: ProviderRuntimeState
    account_id: SidekickAccountId | None
    provider_identity: ProviderIdentity | None
    runtime_generation: AuthorityGeneration | None
    verified_at: datetime
    outcome: ActivationOutcome

    def __post_init__(self) -> None:
        """Require a complete state-specific provider observation."""
        object.__setattr__(self, "verified_at", as_utc(self.verified_at))
        if self.runtime_state is ProviderRuntimeState.SAVED_ACTIVE:
            if (
                self.account_id is None
                or self.provider_identity is None
                or self.runtime_generation is None
                or self.outcome
                not in {
                    ActivationOutcome.VERIFIED,
                    ActivationOutcome.ROLLED_BACK,
                    ActivationOutcome.EXTERNAL_RECONCILED,
                }
            ):
                raise ValueError(
                    "Saved-active state requires complete verified identity."
                )
            return
        if self.runtime_state is ProviderRuntimeState.EXTERNAL_ACTIVE:
            if (
                self.account_id is not None
                or self.provider_identity is None
                or self.runtime_generation is None
                or self.outcome is not ActivationOutcome.EXTERNAL_RECONCILED
            ):
                raise ValueError(
                    "External-active state requires unowned provider identity."
                )
            return
        if (
            self.account_id is not None
            or self.provider_identity is not None
            or self.runtime_generation is not None
        ):
            raise ValueError(
                "Inactive provider state cannot claim account identity."
            )
        expected = {
            ProviderRuntimeState.LOGGED_OUT: ActivationOutcome.LOGGED_OUT,
            ProviderRuntimeState.UNREADABLE: (
                ActivationOutcome.RECONCILIATION_REQUIRED
            ),
            ProviderRuntimeState.UNSUPPORTED: ActivationOutcome.UNSUPPORTED,
        }[self.runtime_state]
        if self.outcome is not expected:
            raise ValueError("Provider runtime state and outcome disagree.")


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivationRecord:
    """One secret-free provider activation journal record."""

    provider_id: ProviderId
    operation_id: OperationId
    source_account_id: SidekickAccountId | None
    target_account_id: SidekickAccountId
    source_provider_identity: ProviderIdentity | None
    source_generation: AuthorityGeneration | None
    expected_target_identity: ProviderIdentity
    phase: ActivationPhase
    started_at: datetime
    updated_at: datetime
    outcome: ActivationOutcome | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        """Normalize timestamps and enforce terminal result invariants."""
        started_at = as_utc(self.started_at)
        updated_at = as_utc(self.updated_at)
        if updated_at < started_at:
            raise ValueError("Activation update cannot predate its start.")
        if (
            self.source_generation is not None
            and self.source_provider_identity is None
        ):
            raise ValueError(
                "Source generation requires a source provider identity."
            )
        if self.source_account_id == self.target_account_id:
            raise ValueError("Activation source and target must differ.")
        _validate_activation_outcome(self.phase, self.outcome)
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(
            self,
            "failure_code",
            _safe_code(self.failure_code),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DueOperation:
    """One durable operation slot keyed by account and operation kind."""

    operation_id: OperationId
    provider_id: ProviderId
    account_id: SidekickAccountId
    kind: OperationKind
    priority: OperationPriority
    state: OperationState
    due_at: datetime
    updated_at: datetime
    attempts: int = 0
    failure_code: str | None = None

    def __post_init__(self) -> None:
        """Normalize wall time and validate retry state."""
        if (
            type(self.attempts) is not int
            or self.attempts < 0
            or self.attempts > _MAX_ATTEMPTS
        ):
            raise ValueError("Operation attempts are outside the bound.")
        due_at = as_utc(self.due_at)
        updated_at = as_utc(self.updated_at)
        failure_code = _safe_code(self.failure_code)
        if (
            self.state
            in {
                OperationState.RETRY_WAIT,
                OperationState.ACTION_REQUIRED,
            }
            and failure_code is None
        ):
            raise ValueError("Failed operation state requires a safe code.")
        if (
            self.state in {OperationState.SCHEDULED, OperationState.RUNNING}
            and failure_code is not None
        ):
            raise ValueError("Healthy operation state cannot carry failure.")
        if self.priority is OperationPriority.CODEX_CALLBACK and (
            self.provider_id is not ProviderId.CODEX
            or self.kind is not OperationKind.REFRESH
        ):
            raise ValueError(
                "Codex callback priority is reserved for Codex refresh."
            )
        object.__setattr__(self, "due_at", due_at)
        object.__setattr__(self, "updated_at", updated_at)
        object.__setattr__(self, "failure_code", failure_code)


_ACTIVATION_TRANSITIONS: dict[
    ActivationPhase,
    frozenset[ActivationPhase],
] = {
    ActivationPhase.PREPARED: frozenset(
        {
            ActivationPhase.OUTGOING_RETAINED,
            ActivationPhase.TARGET_ACTIVATED,
            ActivationPhase.ROLLED_BACK,
            ActivationPhase.RECONCILIATION_REQUIRED,
        }
    ),
    ActivationPhase.OUTGOING_RETAINED: frozenset(
        {
            ActivationPhase.TARGET_ACTIVATED,
            ActivationPhase.ROLLED_BACK,
            ActivationPhase.RECONCILIATION_REQUIRED,
        }
    ),
    ActivationPhase.TARGET_ACTIVATED: frozenset(
        {
            ActivationPhase.READ_BACK_VERIFIED,
            ActivationPhase.ROLLED_BACK,
            ActivationPhase.RECONCILIATION_REQUIRED,
        }
    ),
    ActivationPhase.READ_BACK_VERIFIED: frozenset(
        {
            ActivationPhase.COMMITTED,
            ActivationPhase.ROLLED_BACK,
            ActivationPhase.RECONCILIATION_REQUIRED,
        }
    ),
    ActivationPhase.COMMITTED: frozenset(),
    ActivationPhase.ROLLED_BACK: frozenset(),
    ActivationPhase.RECONCILIATION_REQUIRED: frozenset(),
}


def _validate_activation_outcome(
    phase: ActivationPhase,
    outcome: ActivationOutcome | None,
) -> None:
    """Require terminal phases to carry one truthful safe outcome."""
    allowed: frozenset[ActivationOutcome | None]
    if phase is ActivationPhase.COMMITTED:
        allowed = frozenset({ActivationOutcome.VERIFIED})
    elif phase is ActivationPhase.ROLLED_BACK:
        allowed = frozenset(
            {
                ActivationOutcome.ROLLED_BACK,
                ActivationOutcome.EXTERNAL_RECONCILED,
                ActivationOutcome.LOGGED_OUT,
            }
        )
    elif phase is ActivationPhase.RECONCILIATION_REQUIRED:
        allowed = frozenset({ActivationOutcome.RECONCILIATION_REQUIRED})
    else:
        allowed = frozenset({None})
    if outcome not in allowed:
        raise ValueError("Activation phase and outcome disagree.")


def transition_activation(
    record: ActivationRecord,
    phase: ActivationPhase,
    *,
    updated_at: datetime,
    outcome: ActivationOutcome | None = None,
    failure_code: str | None = None,
) -> ActivationRecord:
    """Advance one activation through exactly one legal phase edge."""
    if phase not in _ACTIVATION_TRANSITIONS[record.phase]:
        raise ValueError("Illegal activation phase transition.")
    normalized_update = max(as_utc(updated_at), record.updated_at)
    defaults = {
        ActivationPhase.COMMITTED: ActivationOutcome.VERIFIED,
        ActivationPhase.ROLLED_BACK: ActivationOutcome.ROLLED_BACK,
        ActivationPhase.RECONCILIATION_REQUIRED: (
            ActivationOutcome.RECONCILIATION_REQUIRED
        ),
    }
    effective_outcome = outcome
    if effective_outcome is None:
        effective_outcome = defaults.get(phase)
    return replace(
        record,
        phase=phase,
        updated_at=normalized_update,
        outcome=effective_outcome,
        failure_code=failure_code,
    )


def decide_activation_recovery(
    record: ActivationRecord,
    read_back: SelectedAccountState,
) -> ActivationRecoveryAction:
    """Choose recovery from actual provider state, never journal preference."""
    if record.provider_id is not read_back.provider_id:
        raise ValueError("Activation and provider read-back do not match.")
    if read_back.runtime_state in {
        ProviderRuntimeState.UNREADABLE,
        ProviderRuntimeState.UNSUPPORTED,
    }:
        action = ActivationRecoveryAction.RECONCILIATION_REQUIRED
    elif (
        read_back.provider_identity == record.expected_target_identity
        and read_back.runtime_state
        in {
            ProviderRuntimeState.SAVED_ACTIVE,
            ProviderRuntimeState.EXTERNAL_ACTIVE,
        }
    ):
        action = ActivationRecoveryAction.COMMIT_VERIFIED
    elif (
        record.source_provider_identity is not None
        and read_back.provider_identity == record.source_provider_identity
    ):
        action = ActivationRecoveryAction.ROLLBACK_VERIFIED
    elif read_back.runtime_state in {
        ProviderRuntimeState.SAVED_ACTIVE,
        ProviderRuntimeState.EXTERNAL_ACTIVE,
    }:
        action = ActivationRecoveryAction.RECONCILE_EXTERNAL
    elif record.source_provider_identity is not None:
        action = ActivationRecoveryAction.REQUEST_OFFICIAL_ROLLBACK
    elif record.phase in {
        ActivationPhase.PREPARED,
        ActivationPhase.OUTGOING_RETAINED,
    }:
        action = ActivationRecoveryAction.CLOSE_FAILED
    else:
        action = ActivationRecoveryAction.RECONCILIATION_REQUIRED
    return action


_OPERATION_TRANSITIONS: dict[
    OperationState,
    frozenset[OperationState],
] = {
    OperationState.SCHEDULED: frozenset({OperationState.RUNNING}),
    OperationState.RUNNING: frozenset(
        {
            OperationState.SCHEDULED,
            OperationState.RETRY_WAIT,
            OperationState.ACTION_REQUIRED,
        }
    ),
    OperationState.RETRY_WAIT: frozenset(
        {OperationState.RUNNING, OperationState.SCHEDULED}
    ),
    OperationState.ACTION_REQUIRED: frozenset({OperationState.SCHEDULED}),
}


def transition_operation(
    operation: DueOperation,
    state: OperationState,
    *,
    updated_at: datetime,
    due_at: datetime | None = None,
    failure_code: str | None = None,
) -> DueOperation:
    """Advance one durable operation through one legal lifecycle edge."""
    if state not in _OPERATION_TRANSITIONS[operation.state]:
        raise ValueError("Illegal durable operation transition.")
    normalized_update = max(as_utc(updated_at), operation.updated_at)
    effective_due_at = operation.due_at if due_at is None else as_utc(due_at)
    attempts = operation.attempts
    if state is OperationState.RUNNING:
        attempts += 1
    elif state is OperationState.SCHEDULED:
        attempts = 0
        failure_code = None
    return replace(
        operation,
        state=state,
        due_at=effective_due_at,
        updated_at=normalized_update,
        attempts=attempts,
        failure_code=failure_code,
    )


def coalesce_due_operation(
    current: DueOperation,
    incoming: DueOperation,
) -> DueOperation:
    """Coalesce one duplicate slot without appending unbounded work."""
    if (
        current.provider_id is not incoming.provider_id
        or current.account_id != incoming.account_id
        or current.kind is not incoming.kind
    ):
        raise ValueError("Only the same durable operation slot can coalesce.")
    if incoming.state is not OperationState.SCHEDULED:
        raise ValueError("Incoming due work must be scheduled.")
    if current.state is OperationState.RUNNING:
        return current
    if (
        current.state is OperationState.ACTION_REQUIRED
        and incoming.priority is OperationPriority.SCHEDULED
    ):
        return current
    if (
        current.state is OperationState.RETRY_WAIT
        and incoming.priority is OperationPriority.SCHEDULED
    ):
        return current
    priority = (
        incoming.priority
        if incoming.priority.rank < current.priority.rank
        else current.priority
    )
    return replace(
        current,
        priority=priority,
        state=OperationState.SCHEDULED,
        due_at=min(current.due_at, incoming.due_at),
        updated_at=max(current.updated_at, incoming.updated_at),
        failure_code=None,
    )


__all__ = [
    "ActivationOutcome",
    "ActivationPhase",
    "ActivationRecord",
    "ActivationRecoveryAction",
    "DueOperation",
    "OperationKind",
    "OperationPriority",
    "OperationState",
    "ProviderRuntimeState",
    "SelectedAccountState",
    "coalesce_due_operation",
    "decide_activation_recovery",
    "transition_activation",
    "transition_operation",
]

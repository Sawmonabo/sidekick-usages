"""Closed types for provider selection and durable operations."""

from enum import StrEnum

__all__ = [
    "ActivationOutcome",
    "ActivationPhase",
    "ActivationRecoveryAction",
    "OperationKind",
    "OperationPriority",
    "OperationState",
    "ProviderRuntimeState",
]


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

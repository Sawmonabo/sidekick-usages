"""Closed types for provider selection and durable operations."""

from enum import StrEnum


class ProviderRuntimeState(StrEnum):
    """Closed provider-native authentication observations."""

    SAVED_ACTIVE = "saved_active"
    EXTERNAL_ACTIVE = "external_active"
    LOGGED_OUT = "logged_out"
    UNREADABLE = "unreadable"
    UNSUPPORTED = "unsupported"


class ProviderAuthState(StrEnum):
    """Closed secret-free native provider authentication states."""

    ACTIVE = "active"
    LOGGED_OUT = "logged_out"
    UNREADABLE = "unreadable"
    UNSUPPORTED = "unsupported"


class ActivationPhase(StrEnum):
    """Closed durable phases for one provider activation transaction."""

    PREPARED = "prepared"
    OUTGOING_RETAINED = "outgoing_retained"
    TARGET_ACTIVATED = "target_activated"
    PROVIDER_PROOF_VERIFIED = "provider_proof_verified"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    RECONCILIATION_REQUIRED = "reconciliation_required"

    @property
    def terminal(self) -> bool:
        """Return whether no later activation phase is legal."""
        return self in {
            ActivationPhase.COMMITTED,
            ActivationPhase.ROLLED_BACK,
        }


class ActivationOutcome(StrEnum):
    """Sanitized result of provider read-back or activation recovery."""

    VERIFIED = "verified"
    ROLLED_BACK = "rolled_back"
    EXTERNAL_RECONCILED = "external_reconciled"
    LOGGED_OUT = "logged_out"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    UNSUPPORTED = "unsupported"


class OperationKind(StrEnum):
    """Closed worker operation kinds."""

    MAINTAIN = "maintain"
    REFRESH = "refresh"
    CODEX_CALLBACK = "codex_callback"
    USAGE = "usage"
    ACTIVITY = "activity"
    LOGIN = "login"
    ACTIVATE = "activate"
    REPAIR = "repair"
    RECONCILE = "reconcile"
    RECONCILE_NATIVE = "reconcile_native"


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

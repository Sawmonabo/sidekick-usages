"""Closed types for provider selection and durable operations."""

from enum import StrEnum

from sidekick_usages.core.identifiers import CanonicalUuid


class ParticipantId(CanonicalUuid):
    """Stable identifier for one integrated local participant."""

    _name = "Participant ID"


class TurnId(CanonicalUuid):
    """Stable identifier for one admitted participant turn."""

    _name = "Turn ID"


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


class SelectionCode(StrEnum):
    """Closed sanitized outcomes for selection and selection refusal."""

    ALREADY_SELECTED = "already_selected"
    SELECTION_SUCCEEDED = "selection_succeeded"
    SELECTION_READY_ADOPTION_PENDING = "selection_ready_adoption_pending"
    TARGET_REFRESH_REQUIRED = "target_refresh_required"
    TARGET_EXPIRED = "target_expired"
    TARGET_REJECTED = "target_rejected"
    TARGET_MALFORMED = "target_malformed"
    TARGET_UNREADABLE = "target_unreadable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNSUPPORTED_PROVIDER_VERSION = "unsupported_provider_version"
    UNSUPPORTED_SESSION_CAPABILITY = "unsupported_session_capability"
    SESSION_CONFIGURATION_REQUIRED = "session_configuration_required"
    UNCOORDINATED_AUTH_MUTATION = "uncoordinated_auth_mutation"
    REMOTE_CONTROL_STATE_INCOMPATIBLE = "remote_control_state_incompatible"
    PARTICIPANT_UNREACHABLE = "participant_unreachable"
    PARTICIPANT_CONFIRMED_DEAD = "participant_confirmed_dead"
    PARTICIPANT_LOST_AFTER_COMMIT = "participant_lost_after_commit"
    REALTIME_SESSION_ACTIVE = "realtime_session_active"
    ACTIVE_OPERATION_TIMEOUT = "active_operation_timeout"
    AUTHORITY_PROOF_FAILED = "authority_proof_failed"
    SELECTION_ROLLED_BACK = "selection_rolled_back"
    SELECTION_RECOVERY_REQUIRED = "selection_recovery_required"


class SelectionPhase(StrEnum):
    """Closed durable phases for one global provider selection."""

    PREVALIDATING = "prevalidating"
    PREPARING = "preparing"
    WAITING_OLD_TURNS = "waiting_old_turns"
    COMMITTING = "committing"
    AWAITING_READY = "awaiting_ready"
    RECOVERING = "recovering"


class SelectionOutcome(StrEnum):
    """Closed terminal outcomes for one global provider selection."""

    READY = "ready"
    FAILED_OLD_EPOCH = "failed_old_epoch"
    PARTICIPANT_LOST_AFTER_COMMIT = "participant_lost_after_commit"
    RECOVERY_REQUIRED = "recovery_required"


class SelectionRecoveryRelation(StrEnum):
    """Provider-owned relation between baseline and target authority."""

    BASELINE_PROVEN = "baseline_proven"
    TARGET_PROVEN = "target_proven"
    UNRESOLVED = "unresolved"


class AuthorityGenerationRelation(StrEnum):
    """Selected runtime generation relative to saved authority truth."""

    CURRENT = "current"
    OLDER = "older"
    NEWER = "newer"
    NOT_SAFELY_COMPARABLE = "not_safely_comparable"


class ActivationPhase(StrEnum):
    """Closed durable phases for one provider activation transaction."""

    PREPARED = "prepared"
    OUTGOING_RETAINED = "outgoing_retained"
    TARGET_ACTIVATED = "target_activated"
    PROVIDER_PROOF_VERIFIED = "provider_proof_verified"
    NATIVE_REPAIR_STARTED = "native_repair_started"
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
    SELECTION_PREVALIDATE = "selection_prevalidate"
    SELECTION_COMMIT = "selection_commit"
    SELECTION_READBACK = "selection_readback"
    CLAUDE_PARTICIPANT_BIND = "claude_participant_bind"

    @property
    def is_selection_worker(self) -> bool:
        """Return whether this is one global-selection worker phase."""
        return self in {
            OperationKind.SELECTION_PREVALIDATE,
            OperationKind.SELECTION_COMMIT,
            OperationKind.SELECTION_READBACK,
            OperationKind.CLAUDE_PARTICIPANT_BIND,
        }


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

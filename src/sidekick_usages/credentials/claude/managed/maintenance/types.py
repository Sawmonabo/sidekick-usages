"""Closed managed-Claude maintenance outcomes."""

from enum import StrEnum

_FAILURE_CODE_PREFIX = "claude_managed_"


class ClaudeManagedOutcome(StrEnum):
    """Closed outcomes from one managed Claude authority operation."""

    HEALTHY = "healthy"
    FIXED_LIFETIME = "fixed_lifetime"
    UNCHANGED = "unchanged"
    LOGIN_REQUIRED = "login_required"
    INCOMPATIBLE = "incompatible"
    MALFORMED = "malformed"
    UNREADABLE = "unreadable"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    TIMED_OUT = "timed_out"
    TRANSIENT = "transient"
    STATE_CHANGED = "state_changed"

    @property
    def succeeded(self) -> bool:
        """Return whether the account remains ready without intervention."""
        return self in {
            ClaudeManagedOutcome.HEALTHY,
            ClaudeManagedOutcome.FIXED_LIFETIME,
        }

    @property
    def action_required(self) -> bool:
        """Return whether the user must repair this credential authority."""
        return self in {
            ClaudeManagedOutcome.INCOMPATIBLE,
            ClaudeManagedOutcome.LOGIN_REQUIRED,
            ClaudeManagedOutcome.MALFORMED,
            ClaudeManagedOutcome.RECONCILIATION_REQUIRED,
            ClaudeManagedOutcome.UNREADABLE,
        }

    @property
    def failure_code(self) -> str:
        """Return the complete sanitized worker and persistence code."""
        return _FAILURE_CODE_PREFIX + self.value

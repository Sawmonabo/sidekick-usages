"""Closed account-runtime diagnostic vocabulary."""

from enum import StrEnum


class NativeAccountRelation(StrEnum):
    """One saved account's relation to current native provider state."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    EXTERNAL = "external"
    LOGGED_OUT = "logged_out"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class DoctorAccountWarning(StrEnum):
    """Account-specific action copy selected by doctor presentation."""

    LOGIN_REQUIRED = "login_required"
    RECONCILIATION_REQUIRED = "reconciliation_required"

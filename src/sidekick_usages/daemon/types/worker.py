"""Closed scalar types for isolated worker coordination."""

from collections.abc import Callable
from enum import StrEnum

type ExitNotifier = Callable[[], None]


class WorkerLaunchFailureCode(StrEnum):
    """Safe isolated-worker launch failures."""

    FEATURE_DISABLED = "feature_disabled"
    EXECUTABLE_MISSING = "executable_missing"
    EXECUTABLE_UNSAFE = "executable_unsafe"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"
    AUTHORITY_BUSY = "authority_busy"
    TERMINATION_FAILED = "termination_failed"


class WorkerOutcome(StrEnum):
    """Closed sanitized outcomes written by isolated workers."""

    SUCCEEDED = "succeeded"
    TRANSIENT_FAILURE = "transient_failure"
    ACTION_REQUIRED = "action_required"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNSUPPORTED = "unsupported"


class WorkerExchangeState(StrEnum):
    """Closed lifecycle of one inherited worker exchange."""

    AWAITING_RESPONSE = "awaiting_response"
    AWAITING_COMPLETION = "awaiting_completion"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class WorkerExchangePhase(StrEnum):
    """Closed ownership phases around worker descriptor inheritance."""

    READY = "ready"
    CLAIMED = "claimed"
    STARTED = "started"

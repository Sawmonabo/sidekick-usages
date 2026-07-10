"""Closed type vocabulary shared across application features."""

import unicodedata
from enum import IntEnum, StrEnum
from typing import Self

_MAX_ACCOUNT_LABEL_BYTES = 512


class AccountLabel(str):
    """Validated account label with exact Unicode identity.

    Labels are intentionally not stripped, case-folded, or Unicode-normalized.
    """

    def __new__(cls, value: str) -> Self:
        """Validate and construct an account label.

        :param value: Exact label value to preserve.
        :returns: Validated label.
        :raises ValueError: If the label violates the account contract.
        """
        if not value:
            raise ValueError("Account labels must not be empty.")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("Account labels must be valid UTF-8.") from error
        if len(encoded) > _MAX_ACCOUNT_LABEL_BYTES:
            raise ValueError(
                "Account labels must not exceed 512 encoded UTF-8 bytes."
            )
        if any(unicodedata.category(char) == "Cc" for char in value):
            raise ValueError(
                "Account labels must not contain control characters."
            )
        return super().__new__(cls, value)


class ProviderId(StrEnum):
    """Supported provider identifiers at string boundaries."""

    CLAUDE = "claude"
    CODEX = "codex"


class RefreshStatus(StrEnum):
    """Closed saved-token refresh outcomes."""

    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"


class HeartbeatStatus(StrEnum):
    """Closed heartbeat operation and persisted outcomes."""

    WARMED = "warmed"
    ACTIVE = "active"
    DISABLED = "disabled"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    ENABLED = "enabled"


class ExpiryState(StrEnum):
    """Provider-neutral classified expiry states."""

    VALID = "valid"
    EXPIRED = "expired"
    UNKNOWN = "unknown"
    INVALID = "invalid"


class ExitCode(IntEnum):
    """Stable application process outcomes.

    ``SYSTEM_ERROR`` covers configuration, provider, and local system
    failures. Scheduler lifecycle failures remain distinct.
    """

    SUCCESS = 0
    MANUAL_ACTION = 1
    SYSTEM_ERROR = 2
    SCHEDULER_ERROR = 3


def highest_exit_code(*codes: ExitCode) -> ExitCode:
    """Return the highest-priority application process outcome."""
    for candidate in (
        ExitCode.SCHEDULER_ERROR,
        ExitCode.SYSTEM_ERROR,
        ExitCode.MANUAL_ACTION,
    ):
        if candidate in codes:
            return candidate
    return ExitCode.SUCCESS

"""Closed outcomes from a Codex account read."""

from enum import StrEnum


class CodexAccountReadFailure(StrEnum):
    """Secret-safe failure classifications from ``account/read``."""

    MISSING = "missing"
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"

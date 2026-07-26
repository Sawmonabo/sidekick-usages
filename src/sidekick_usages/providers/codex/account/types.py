"""Closed Codex account protocol values."""

from enum import StrEnum


class CodexAuthMode(StrEnum):
    """Supported ChatGPT authentication modes from the official app server."""

    CHATGPT = "chatgpt"
    CHATGPT_AUTH_TOKENS = "chatgptAuthTokens"


class CodexAccountReadFailure(StrEnum):
    """Secret-safe failure classifications from ``account/read``."""

    MISSING = "missing"
    MALFORMED = "malformed"
    UNSUPPORTED = "unsupported"

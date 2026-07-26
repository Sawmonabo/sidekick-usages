"""Immutable official Claude login models and outcomes."""

from dataclasses import dataclass
from enum import StrEnum


class ClaudeOfficialLoginResult(StrEnum):
    """Secret-safe outcome from one official Claude login process."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ClaudeAuthStatus:
    """Bounded non-secret state from the official auth-status command."""

    return_code: int
    logged_in: bool
    auth_method: str
    api_provider: str

"""Claude subscription-login lifetime policy."""

from datetime import datetime, timedelta
from enum import StrEnum, auto

from sidekick_usages.core.expiry import (
    ExpiredExpiry,
    InvalidExpiry,
    UnknownExpiry,
    ValidExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import ClaudeLoginCredentials, Credentials

CLAUDE_LOGIN_RENEWAL_WINDOW = timedelta(days=5)


class ClaudeLoginRenewalState(StrEnum):
    """Secret-free renewal state derived from one credential snapshot."""

    NOT_APPLICABLE = auto()
    UNKNOWN = auto()
    CURRENT = auto()
    RENEWAL_DUE = auto()
    EXPIRED = auto()
    INVALID = auto()


def classify_claude_login_renewal(
    credentials: Credentials,
    *,
    reference_time: datetime,
) -> ClaudeLoginRenewalState:
    """Classify the independent Claude login-renewal lifetime."""
    if not isinstance(credentials, ClaudeLoginCredentials):
        return ClaudeLoginRenewalState.NOT_APPLICABLE
    expiry = classify_expiry(
        credentials.refresh_expiry,
        now=reference_time,
    )
    if isinstance(expiry, UnknownExpiry):
        return ClaudeLoginRenewalState.UNKNOWN
    if isinstance(expiry, InvalidExpiry):
        return ClaudeLoginRenewalState.INVALID
    if isinstance(expiry, ExpiredExpiry):
        return ClaudeLoginRenewalState.EXPIRED
    if isinstance(expiry, ValidExpiry):
        if expiry.at <= reference_time + CLAUDE_LOGIN_RENEWAL_WINDOW:
            return ClaudeLoginRenewalState.RENEWAL_DUE
        return ClaudeLoginRenewalState.CURRENT
    raise AssertionError(f"Unexpected expiry classification: {expiry!r}")

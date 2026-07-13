"""Credential-kind and lifetime classification for doctor output."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, auto
from typing import assert_never

from sidekick_usages.core.expiry import (
    ClassifiedExpiry,
    ExpiredExpiry,
    InvalidExpiry,
    UnknownExpiry,
    ValidExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.credentials.claude_lifetime import (
    ClaudeLoginRenewalState,
    classify_claude_login_renewal,
)

_SECONDS_PER_HOUR = 3_600
_SECONDS_PER_DAY = 86_400


class DoctorCredentialKind(StrEnum):
    """Stable credential-kind values exposed by doctor JSON."""

    SETUP_TOKEN = auto()
    SUBSCRIPTION_LOGIN = auto()
    CODEX_LOGIN = auto()


class IdentityState(StrEnum):
    """Secret-safe stable-identity availability."""

    KNOWN = "known"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CredentialDiagnostic:
    """Classified secret-safe credential state for one account."""

    kind: DoctorCredentialKind
    access_expiry: ClassifiedExpiry
    refresh_expiry: ClassifiedExpiry
    login_renewal_state: ClaudeLoginRenewalState
    identity_state: IdentityState
    can_auto_refresh: bool


def diagnose_credentials(
    account: Account,
    *,
    reference_time: datetime,
    provider_registered: bool,
) -> CredentialDiagnostic:
    """Classify one credential variant without exposing identity values."""
    credentials = account.credentials
    if isinstance(credentials, ClaudeSetupTokenCredentials):
        return CredentialDiagnostic(
            DoctorCredentialKind.SETUP_TOKEN,
            UnknownExpiry(),
            UnknownExpiry(),
            ClaudeLoginRenewalState.NOT_APPLICABLE,
            IdentityState.UNAVAILABLE,
            False,
        )
    if isinstance(credentials, ClaudeLoginCredentials):
        access = classify_expiry(
            credentials.access_expiry,
            now=reference_time,
        )
        refresh = classify_expiry(
            credentials.refresh_expiry,
            now=reference_time,
        )
        return CredentialDiagnostic(
            DoctorCredentialKind.SUBSCRIPTION_LOGIN,
            access,
            refresh,
            classify_claude_login_renewal(
                credentials,
                reference_time=reference_time,
            ),
            (
                IdentityState.KNOWN
                if credentials.identity is not None
                else IdentityState.UNAVAILABLE
            ),
            provider_registered
            and not isinstance(refresh, ExpiredExpiry | InvalidExpiry),
        )
    if isinstance(credentials, CodexCredentials):
        return CredentialDiagnostic(
            DoctorCredentialKind.CODEX_LOGIN,
            classify_expiry(credentials.expiry, now=reference_time),
            UnknownExpiry(),
            ClaudeLoginRenewalState.NOT_APPLICABLE,
            (
                IdentityState.KNOWN
                if credentials.account_id is not None
                else IdentityState.UNAVAILABLE
            ),
            provider_registered and credentials.refresh_token is not None,
        )
    assert_never(credentials)


def authentication_label(kind: DoctorCredentialKind) -> str:
    """Return the stable product label for one credential kind."""
    if kind is DoctorCredentialKind.SETUP_TOKEN:
        return "setup token"
    if kind is DoctorCredentialKind.SUBSCRIPTION_LOGIN:
        return "subscription login"
    return "Codex login"


def access_expiry_display(
    kind: DoctorCredentialKind,
    expiry: ClassifiedExpiry,
    reference_time: datetime,
) -> str:
    """Return concise human access-expiry copy."""
    if kind is DoctorCredentialKind.SETUP_TOKEN:
        display = "unavailable"
    elif isinstance(expiry, InvalidExpiry):
        display = "invalid"
    elif isinstance(expiry, ExpiredExpiry):
        display = "expired"
    elif not isinstance(expiry, ValidExpiry):
        display = "unavailable"
    else:
        seconds = int((expiry.at - reference_time).total_seconds())
        if seconds < _SECONDS_PER_HOUR:
            display = f"in {seconds // 60}m"
        elif seconds < _SECONDS_PER_DAY:
            hours, minutes = divmod(seconds // 60, 60)
            display = f"in {hours}h {minutes}m"
        else:
            days, remainder = divmod(seconds, _SECONDS_PER_DAY)
            display = f"in {days}d {remainder // _SECONDS_PER_HOUR}h"
    return display


def refresh_expiry_display(expiry: ClassifiedExpiry) -> str:
    """Return human login-lifetime copy without exposing identity."""
    if isinstance(expiry, InvalidExpiry):
        return "invalid"
    if not isinstance(expiry, ValidExpiry | ExpiredExpiry):
        return "unavailable"
    local = expiry.at.astimezone()
    date = local.strftime("%b ") + str(local.day) + local.strftime(", %Y")
    return f"{date} (expired)" if isinstance(expiry, ExpiredExpiry) else date


def expiry_time(expiry: ClassifiedExpiry) -> datetime | None:
    """Return the authoritative time from a classified expiry."""
    if isinstance(expiry, ValidExpiry | ExpiredExpiry):
        return expiry.at
    return None


__all__ = [
    "CredentialDiagnostic",
    "DoctorCredentialKind",
    "IdentityState",
    "access_expiry_display",
    "authentication_label",
    "diagnose_credentials",
    "expiry_time",
    "refresh_expiry_display",
]

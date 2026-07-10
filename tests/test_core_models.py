"""Load-bearing invariants for provider-neutral account models."""

from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta, timezone

import pytest

from sidekick_usages.core.expiry import (
    ExpiredExpiry,
    InvalidExpiry,
    KnownExpiry,
    UnknownExpiry,
    ValidExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
    DetectedCredentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId

REFERENCE_TIME = datetime(2026, 7, 10, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "is_valid"),
    [
        ("\N{LATIN SMALL LETTER E WITH ACUTE}", True),
        ("e\N{COMBINING ACUTE ACCENT}", True),
        ("", False),
        ("contains\ncontrol", False),
        ("contains\x00nul", False),
        ("\ud800", False),
        ("x" * 513, False),
    ],
)
def test_account_labels_preserve_exact_unicode_and_reject_invalid_values(
    value: str,
    *,
    is_valid: bool,
) -> None:
    """Labels preserve identity without admitting unsafe stored keys."""
    if is_valid:
        assert AccountLabel(value) == value
    else:
        with pytest.raises(ValueError, match="Account labels"):
            AccountLabel(value)


@pytest.mark.parametrize(
    ("expiry", "expected_type"),
    [
        (
            KnownExpiry(REFERENCE_TIME - timedelta(microseconds=1)),
            ExpiredExpiry,
        ),
        (KnownExpiry(REFERENCE_TIME), ExpiredExpiry),
        (
            KnownExpiry(REFERENCE_TIME + timedelta(microseconds=1)),
            ValidExpiry,
        ),
        (UnknownExpiry(), UnknownExpiry),
        (InvalidExpiry(), InvalidExpiry),
    ],
)
def test_expiry_classification_has_one_explicit_boundary(
    expiry: KnownExpiry | UnknownExpiry | InvalidExpiry,
    expected_type: type[
        ValidExpiry | ExpiredExpiry | UnknownExpiry | InvalidExpiry
    ],
) -> None:
    """Known, unknown, and invalid expiry states remain distinguishable."""
    assert isinstance(
        classify_expiry(expiry, now=REFERENCE_TIME),
        expected_type,
    )


def test_timed_expiry_and_mutable_account_times_require_aware_datetimes() -> (
    None
):
    """Naive values cannot enter timed expiry or mutable account state."""
    naive = REFERENCE_TIME.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone-aware"):
        KnownExpiry(naive)

    offset_time = REFERENCE_TIME.astimezone(timezone(timedelta(hours=-4)))
    resets = {"standard": offset_time}
    account = Account(
        label=AccountLabel("claude-team"),
        credentials=ClaudeCredentials(access_token="synthetic-access"),
        heartbeat_window_resets=resets,
    )
    resets["standard"] = naive
    assert account.heartbeat_window_resets == {"standard": REFERENCE_TIME}
    assert not isinstance(account.heartbeat_window_resets, MutableMapping)
    account.last_refresh_at = offset_time
    assert account.last_refresh_at == REFERENCE_TIME
    with pytest.raises(ValueError, match="timezone-aware"):
        account.last_refresh_at = naive
    assert account.last_refresh_at == REFERENCE_TIME
    assert not hasattr(UnknownExpiry(), "at")
    assert not hasattr(InvalidExpiry(), "at")


def test_provider_identity_is_derived_and_representations_hide_secrets() -> (
    None
):
    """Credential variants determine identity without exposing tokens."""
    credentials = (
        ClaudeCredentials(
            access_token="claude-access-secret",
            refresh_token="claude-refresh-secret",
        ),
        CodexCredentials(
            access_token="codex-access-secret",
            refresh_token="codex-refresh-secret",
            id_token="codex-id-secret",
        ),
    )

    assert tuple(item.provider_id for item in credentials) == (
        ProviderId.CLAUDE,
        ProviderId.CODEX,
    )
    for index, item in enumerate(credentials):
        account = Account(
            label=AccountLabel(f"account-{index}"),
            credentials=item,
        )
        detected = DetectedCredentials(item)
        rendered = repr((account, detected))
        assert account.provider_id is item.provider_id
        assert item.access_token not in rendered
        if item.refresh_token is not None:
            assert item.refresh_token not in rendered
        if isinstance(item, CodexCredentials) and item.id_token is not None:
            assert item.id_token not in rendered

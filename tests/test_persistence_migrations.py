"""Pure transformation tests for account persistence generations."""

import json
from datetime import UTC, datetime

import pytest

from sidekick_usages.core.expiry import InvalidExpiry, KnownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.persistence.errors import (
    InvalidSchemaError,
    RollbackCompatibilityError,
)
from sidekick_usages.persistence.migrations import (
    accounts_to_version_one,
    generation_zero_to_version_one,
    version_one_to_accounts,
    version_one_to_v060,
)
from sidekick_usages.persistence.schemas import (
    GenerationZeroDocument,
    StoredAccountRecord,
    VersionOneDocument,
    decode_generation_zero,
    decode_version_one,
    encode_generation_zero,
    encode_version_one,
)

EXPIRY = datetime(2026, 7, 11, 12, tzinfo=UTC)
CLAUDE_EXPIRY_MILLISECONDS = 1_783_771_200_000
CODEX_EXPIRY_SECONDS = 1_783_771_200


def _stored_record(
    provider_id: ProviderId,
    *,
    targets: tuple[str, ...] | None = None,
    resets: tuple[tuple[str, datetime], ...] | None = None,
) -> StoredAccountRecord:
    is_claude = provider_id is ProviderId.CLAUDE
    return StoredAccountRecord(
        label=AccountLabel(f"{provider_id}-account"),
        provider_id=provider_id,
        provider_account_id=None if is_claude else "acct_test_only",
        access_token=f"test-only-{provider_id}-access",
        refresh_token=f"test-only-{provider_id}-refresh",
        expires_at=EXPIRY,
        plan="max" if is_claude else "plus",
        scopes=("user:profile",) if is_claude else None,
        codex_home=None if is_claude else "/synthetic/codex",
        codex_id_token=None if is_claude else "test-only-id-token",
        codex_last_refresh=None,
        last_refresh_at=None,
        last_refresh_status=None,
        last_refresh_error=None,
        heartbeat_enabled=not is_claude,
        heartbeat_5h_reset_at=None,
        heartbeat_window_resets=resets,
        heartbeat_targets=targets,
        last_heartbeat_at=None,
        last_heartbeat_status=None,
        last_heartbeat_error=None,
    )


def test_forward_and_v060_reverse_preserve_both_provider_units() -> None:
    """Normalized state emits canonical v1 and exact v0.6 epoch units."""
    source = GenerationZeroDocument(
        (
            _stored_record(ProviderId.CLAUDE),
            _stored_record(ProviderId.CODEX),
        )
    )

    version_one = generation_zero_to_version_one(source)
    encoded = encode_version_one(version_one)
    assert encode_version_one(decode_version_one(encoded)) == encoded

    reverse = version_one_to_v060(decode_version_one(encoded))
    reverse_bytes = encode_generation_zero(reverse)
    assert decode_generation_zero(reverse_bytes) == source
    reverse_json = json.loads(reverse_bytes)
    assert (
        reverse_json["claude-account"]["expires_at"]
        == CLAUDE_EXPIRY_MILLISECONDS
    )
    assert reverse_json["codex-account"]["expires_at"] == CODEX_EXPIRY_SECONDS


@pytest.mark.parametrize(
    ("targets", "resets", "compatible"),
    [
        (None, None, True),
        ((), None, False),
        (("standard",), None, True),
        (None, (), False),
        (None, (("standard", EXPIRY),), True),
    ],
)
def test_v060_reverse_rejects_only_unrepresentable_empty_collections(
    targets: tuple[str, ...] | None,
    resets: tuple[tuple[str, datetime], ...] | None,
    *,
    compatible: bool,
) -> None:
    """Rollback never silently collapses explicit empty state to unknown."""
    document = VersionOneDocument(
        (
            _stored_record(
                ProviderId.CODEX,
                targets=targets,
                resets=resets,
            ),
        )
    )

    if compatible:
        assert isinstance(
            version_one_to_v060(document),
            GenerationZeroDocument,
        )
    else:
        with pytest.raises(RollbackCompatibilityError) as exc_info:
            version_one_to_v060(document)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None


def test_runtime_account_conversion_is_validated_and_secret_safe() -> None:
    """The persistence DTO neither leaks tokens nor trusts invalid expiry."""
    accounts = (
        Account(
            label=AccountLabel("claude-max-1"),
            credentials=ClaudeCredentials(
                access_token="claude-access-secret",
                refresh_token="claude-refresh-secret",
                expiry=KnownExpiry(EXPIRY),
                scopes=("user:profile",),
            ),
            plan="max",
        ),
        Account(
            label=AccountLabel("codex-plus-1"),
            credentials=CodexCredentials(
                access_token="codex-access-secret",
                refresh_token="codex-refresh-secret",
                expiry=KnownExpiry(EXPIRY),
                account_id="acct_test_only",
                id_token="codex-id-secret",
            ),
            plan="plus",
        ),
    )

    document = accounts_to_version_one(accounts)
    assert version_one_to_accounts(document) == accounts
    representation = repr(document)
    assert all(
        secret not in representation
        for secret in (
            "claude-access-secret",
            "claude-refresh-secret",
            "codex-access-secret",
            "codex-refresh-secret",
            "codex-id-secret",
        )
    )

    invalid = Account(
        label=AccountLabel("invalid"),
        credentials=ClaudeCredentials(
            access_token="test-only-token",
            expiry=InvalidExpiry(),
        ),
    )
    with pytest.raises(InvalidSchemaError):
        accounts_to_version_one((invalid,))

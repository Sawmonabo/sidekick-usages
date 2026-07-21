"""Strict schema-version-two account persistence contracts."""

import json

import pytest

from sidekick_usages.persistence.errors import (
    DuplicateCredentialOwnershipError,
    InvalidSchemaError,
)
from sidekick_usages.persistence.schemas import (
    decode_authority,
    decode_prototype_receipt,
    encode_prototype_receipt,
)


def _state_fields() -> dict[str, object]:
    return {
        "last_refresh_at": None,
        "last_refresh_status": None,
        "last_refresh_error": None,
        "heartbeat_enabled": False,
        "heartbeat_5h_reset_at": None,
        "heartbeat_window_resets": None,
        "heartbeat_targets": None,
        "last_heartbeat_at": None,
        "last_heartbeat_status": None,
        "last_heartbeat_error": None,
    }


def _setup_record() -> dict[str, object]:
    return {
        "provider_id": "claude",
        "credential_kind": "setup_token",
        "access_token": "test-only-setup-access",
        "plan": "team",
        **_state_fields(),
    }


def _login_record() -> dict[str, object]:
    return {
        "provider_id": "claude",
        "credential_kind": "subscription_login",
        "access_token": "test-only-login-access",
        "refresh_token": "test-only-login-refresh",
        "access_expires_at": "2026-07-12T12:25:46.019000Z",
        "refresh_expires_at": "2026-12-01T00:00:00.000000Z",
        "scopes": ["user:inference", "user:profile"],
        "claude_identity": {
            "account_id": "test-only-account-id",
            "organization_id": "test-only-organization-id",
        },
        "plan": "team",
        **_state_fields(),
    }


def _payload(*records: tuple[str, dict[str, object]]) -> bytes:
    return json.dumps({"schema_version": 2, "accounts": dict(records)}).encode(
        "utf-8"
    )


def test_current_schema_accepts_both_strict_claude_variants() -> None:
    """The current envelope admits each complete discriminated record."""
    document = decode_authority(
        _payload(
            ("claude-setup", _setup_record()),
            ("claude-login", _login_record()),
        )
    )

    assert [str(account.label) for account in document.accounts] == [
        "claude-setup",
        "claude-login",
    ]


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("refresh_token", "test-only-refresh"),
        ("access_expires_at", "2026-07-12T12:25:46.019000Z"),
        ("refresh_expires_at", None),
        ("scopes", ["user:profile"]),
        (
            "claude_identity",
            {
                "account_id": "test-only-account-id",
                "organization_id": "test-only-organization-id",
            },
        ),
    ],
)
def test_setup_record_forbids_every_login_only_field(
    field_name: str,
    value: object,
) -> None:
    """A setup token cannot carry subscription-login state."""
    record = _setup_record()
    record[field_name] = value

    with pytest.raises(InvalidSchemaError):
        decode_authority(_payload(("claude-setup", record)))


@pytest.mark.parametrize(
    "field_name",
    [
        "refresh_token",
        "access_expires_at",
        "refresh_expires_at",
        "scopes",
    ],
)
def test_login_record_requires_every_operational_member(
    field_name: str,
) -> None:
    """A subscription login cannot persist partial operational state."""
    record = _login_record()
    del record[field_name]

    with pytest.raises(InvalidSchemaError):
        decode_authority(_payload(("claude-login", record)))


@pytest.mark.parametrize(
    "scopes",
    [
        [],
        ["user:inference"],
    ],
)
def test_login_record_requires_strict_profile_scopes(
    scopes: list[str],
) -> None:
    """Invalid login scopes fail at the typed persistence boundary."""
    record = _login_record()
    record["scopes"] = scopes

    with pytest.raises(InvalidSchemaError):
        decode_authority(_payload(("claude-login", record)))


@pytest.mark.parametrize("identity_member", ["account_id", "organization_id"])
def test_login_identity_is_absent_or_complete(identity_member: str) -> None:
    """One nested identity never admits independently nullable members."""
    record = _login_record()
    identity = {
        "account_id": "test-only-account-id",
        "organization_id": "test-only-organization-id",
    }
    del identity[identity_member]
    record["claude_identity"] = identity

    with pytest.raises(InvalidSchemaError):
        decode_authority(_payload(("claude-login", record)))


def test_current_schema_forbids_unknown_fields_at_every_boundary() -> None:
    """Envelope, record, and nested identity reject unowned fields."""
    root = json.loads(_payload(("claude-login", _login_record())))
    root["unexpected"] = False
    record = root["accounts"]["claude-login"]
    record["unexpected"] = False
    record["claude_identity"]["unexpected"] = False

    with pytest.raises(InvalidSchemaError):
        decode_authority(json.dumps(root).encode("utf-8"))


@pytest.mark.parametrize("field_name", ["access_token", "refresh_token"])
def test_current_schema_rejects_duplicate_credential_ownership(
    field_name: str,
) -> None:
    """One provider credential has exactly one durable label owner."""
    first = _login_record()
    second = _login_record()
    second["access_token"] = "test-only-second-access"
    second["refresh_token"] = "test-only-second-refresh"
    second[field_name] = first[field_name]

    with pytest.raises(DuplicateCredentialOwnershipError) as exc_info:
        decode_authority(
            _payload(
                ("claude-first", first),
                ("claude-second", second),
            )
        )

    assert exc_info.value.labels == ("claude-first", "claude-second")
    representation = repr(exc_info.value)
    assert str(first[field_name]) not in representation
    assert "sha256" not in representation.lower()


def test_version_one_prototype_receipt_remains_strictly_readable() -> None:
    """Existing lineage remains valid without becoming current evidence."""
    digest = "a" * 64
    payload = (
        "{\n"
        '  "receipt_version": 1,\n'
        f'  "prototype_sha256": "{digest}",\n'
        '  "target_schema_version": 1\n'
        "}\n"
    ).encode()

    receipt = decode_prototype_receipt(payload)

    assert receipt.target_schema_version == 1
    assert encode_prototype_receipt(receipt) == payload

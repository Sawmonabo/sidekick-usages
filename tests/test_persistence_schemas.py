"""Load-bearing tests for persisted account schemas and pure transforms."""

import json
from datetime import UTC, datetime

import pytest

from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import (
    DuplicateKeyError,
    FutureSchemaError,
    InvalidSchemaError,
    MalformedJsonError,
    SchemaIssue,
    SchemaIssueCode,
)
from sidekick_usages.persistence.schemas import (
    MAX_ACCOUNTS,
    MAX_DOCUMENT_BYTES,
    GenerationZeroDocument,
    PrototypeReceipt,
    VersionOneDocument,
    decode_authority,
    decode_generation_zero,
    decode_prototype,
    decode_prototype_receipt,
    decode_version_one,
    encode_generation_zero,
    encode_prototype_receipt,
    encode_version_one,
)
from sidekick_usages.persistence.transforms import prototype_to_version_one
from sidekick_usages.serialization import JsonObject, JsonValue

EXPIRY = "2026-07-11T12:00:00.000000Z"
AUDIT_TIME = "2026-07-10T12:00:00.000000Z"
CLAUDE_EXPIRY_MILLISECONDS = 1_783_771_200_000
CODEX_EXPIRY_SECONDS = 1_783_771_200
FUTURE_SCHEMA_VERSION = 3


def _payload(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


def _record(
    provider_id: ProviderId,
    *,
    version_one: bool = True,
) -> JsonObject:
    is_claude = provider_id is ProviderId.CLAUDE
    expires_at: JsonValue
    if version_one:
        expires_at = EXPIRY
    elif is_claude:
        expires_at = CLAUDE_EXPIRY_MILLISECONDS
    else:
        expires_at = CODEX_EXPIRY_SECONDS
    return {
        "provider_id": provider_id.value,
        "provider_account_id": None if is_claude else "acct_test_only",
        "access_token": f"test-only-{provider_id}-access-token",
        "refresh_token": f"test-only-{provider_id}-refresh-token",
        "expires_at": expires_at,
        "plan": "max" if is_claude else "plus",
        "scopes": ["user:profile"] if is_claude else None,
        "codex_home": None if is_claude else "/synthetic/codex/account",
        "codex_id_token": None if is_claude else "test-only-id-token",
        "codex_last_refresh": None if is_claude else AUDIT_TIME,
        "last_refresh_at": AUDIT_TIME,
        "last_refresh_status": "ok",
        "last_refresh_error": None,
        "heartbeat_enabled": not is_claude,
        "heartbeat_5h_reset_at": None if is_claude else EXPIRY,
        "heartbeat_window_resets": (
            None if is_claude else {"standard": EXPIRY}
        ),
        "heartbeat_targets": None if is_claude else ["standard"],
        "last_heartbeat_at": None if is_claude else AUDIT_TIME,
        "last_heartbeat_status": None if is_claude else "active",
        "last_heartbeat_error": None,
    }


def _version_one_root() -> JsonObject:
    return {
        "schema_version": 1,
        "accounts": {
            "claude-max-1": _record(ProviderId.CLAUDE),
            "codex-plus-1": _record(ProviderId.CODEX),
        },
    }


def _account_record(root: JsonObject, label: str) -> JsonObject:
    accounts = root["accounts"]
    assert isinstance(accounts, dict)
    record = accounts[label]
    assert isinstance(record, dict)
    return record


def _generation_zero_root() -> JsonObject:
    return {
        "claude-max-1": _record(
            ProviderId.CLAUDE,
            version_one=False,
        ),
        "codex-plus-1": _record(
            ProviderId.CODEX,
            version_one=False,
        ),
    }


def _generation_zero_at_byte_limit() -> bytes:
    root: JsonObject = {}
    records: list[JsonObject] = []
    for index in range(MAX_ACCOUNTS):
        record = _record(ProviderId.CLAUDE, version_one=False)
        root[f"account-{index}"] = record
        records.append(record)
    remaining = MAX_DOCUMENT_BYTES - len(_payload(root))
    for record in records:
        for field_name in ("access_token", "refresh_token"):
            value = record[field_name]
            assert isinstance(value, str)
            growth = min(remaining, 262_144 - len(value.encode()))
            record[field_name] = value + "x" * growth
            remaining -= growth
            if remaining == 0:
                payload = _payload(root)
                assert len(payload) == MAX_DOCUMENT_BYTES
                return payload
    raise AssertionError("Token capacity did not reach the document limit.")


def test_encoder_preserves_account_order_and_exact_record_shape() -> None:
    """Canonical output never sorts labels or omits explicit null fields."""
    root = _version_one_root()
    accounts = root["accounts"]
    assert isinstance(accounts, dict)
    accounts["account-\N{LATIN SMALL LETTER E WITH ACUTE}"] = accounts.pop(
        "claude-max-1"
    )
    document = decode_version_one(_payload(root))
    reversed_document = VersionOneDocument(tuple(reversed(document.accounts)))

    encoded = encode_version_one(reversed_document)
    decoded = json.loads(encoded)

    assert encoded.startswith(b'{\n  "schema_version": 1,\n  "accounts": {')
    assert encoded.endswith(b"\n")
    assert encode_version_one(decode_version_one(encoded)) == encoded
    assert list(decoded) == ["schema_version", "accounts"]
    assert list(decoded["accounts"]) == [
        "account-\N{LATIN SMALL LETTER E WITH ACUTE}",
        "codex-plus-1",
    ]
    assert list(
        decoded["accounts"]["account-\N{LATIN SMALL LETTER E WITH ACUTE}"]
    ) == list(_record(ProviderId.CLAUDE))
    assert (
        decoded["accounts"]["account-\N{LATIN SMALL LETTER E WITH ACUTE}"][
            "codex_home"
        ]
        is None
    )
    assert b"account-\xc3\xa9" in encoded
    assert b"\\u00e9" not in encoded


@pytest.mark.parametrize(
    "case",
    [
        "envelope-extra",
        "record-extra",
        "record-missing",
        "string-boolean",
        "non-object-account",
    ],
)
def test_version_one_envelope_and_records_are_exact(case: str) -> None:
    """Current state accepts no coercion, omitted field, or extra member."""
    root = _version_one_root()
    if case == "envelope-extra":
        root["extra"] = True
    elif case == "record-extra":
        _account_record(root, "claude-max-1")["extra"] = True
    elif case == "record-missing":
        _account_record(root, "claude-max-1").pop("plan")
    elif case == "string-boolean":
        _account_record(root, "claude-max-1")["heartbeat_enabled"] = "false"
    else:
        accounts = root["accounts"]
        assert isinstance(accounts, dict)
        accounts["claude-max-1"] = "not-an-object"

    with pytest.raises(InvalidSchemaError):
        decode_version_one(_payload(root))


def test_schema_diagnostics_project_paths_without_rejected_input() -> None:
    """Pydantic details become bounded Sidekick-owned repair categories."""
    secrets = (
        "synthetic-access-secret",
        "synthetic-refresh-secret",
        "synthetic-id-secret",
    )
    root = _version_one_root()
    record = _account_record(root, "codex-plus-1")
    record["access_token"] = secrets[0]
    record["refresh_token"] = secrets[1]
    record["codex_id_token"] = secrets[2]
    record.pop("plan")
    record[secrets[0]] = True
    record["heartbeat_enabled"] = "false"

    with pytest.raises(InvalidSchemaError) as exc_info:
        decode_version_one(_payload(root))

    error = exc_info.value
    assert {type(issue) for issue in error.issues} == {SchemaIssue}
    codes = {issue.code for issue in error.issues}
    assert SchemaIssueCode.MISSING_FIELD in codes
    assert SchemaIssueCode.UNEXPECTED_FIELD in codes
    assert SchemaIssueCode.INVALID_TYPE in codes
    assert any(issue.path[-1] == "plan" for issue in error.issues)
    assert any(issue.path[-1] == "heartbeat_enabled" for issue in error.issues)
    assert all(secret not in repr(error.issues) for secret in secrets)
    assert error.__cause__ is None
    assert error.__context__ is None


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        (b"\xff", MalformedJsonError),
        (b"\xef\xbb\xbf{}", MalformedJsonError),
        (b'{"broken":', MalformedJsonError),
        (b'{"value":NaN}', MalformedJsonError),
        (b'{"value":1e999}', MalformedJsonError),
        (b'{"same":1,"same":2}', DuplicateKeyError),
        (
            b'{"a":{"provider_id":"claude","provider_id":"claude"}}',
            DuplicateKeyError,
        ),
        (
            b'{"schema_version":3,"accounts":{}}',
            FutureSchemaError,
        ),
        (b'{"schema_version":true,"accounts":{}}', InvalidSchemaError),
        (b"[]", InvalidSchemaError),
    ],
    ids=[
        "invalid-utf8",
        "bom",
        "syntax",
        "nan",
        "overflowing-number",
        "root-duplicate",
        "nested-duplicate",
        "future",
        "boolean-version",
        "wrong-root",
    ],
)
def test_codec_failures_keep_lexical_and_schema_states_distinct(
    payload: bytes,
    error_type: type[Exception],
) -> None:
    """Assessment can classify corruption without parser error leakage."""
    with pytest.raises(error_type) as exc_info:
        decode_authority(payload)

    error = exc_info.value
    assert error.__cause__ is None
    assert error.__context__ is None
    if isinstance(error, FutureSchemaError):
        assert error.schema_version == FUTURE_SCHEMA_VERSION


def test_object_valued_schema_version_label_remains_generation_zero() -> None:
    """The reserved envelope member does not steal a historical label."""
    root: JsonObject = {
        "schema_version": {
            "provider_id": "claude",
            "access_token": "test-only-token",
            "refresh_token": None,
            "expires_at": None,
            "plan": "max",
        }
    }

    document = decode_authority(_payload(root))

    assert isinstance(document, GenerationZeroDocument)
    assert document.accounts[0].label == "schema_version"


@pytest.mark.parametrize(
    ("label", "field_name", "valid_value", "invalid_value"),
    [
        (
            "claude-max-1",
            "access_token",
            "x" * 262_144,
            "x" * 262_145,
        ),
        (
            "claude-max-1",
            "refresh_token",
            "x" * 262_144,
            "x" * 262_145,
        ),
        (
            "codex-plus-1",
            "codex_id_token",
            "x" * 262_144,
            "x" * 262_145,
        ),
        (
            "codex-plus-1",
            "provider_account_id",
            "x" * 4_096,
            "x" * 4_097,
        ),
        ("claude-max-1", "plan", "é" * 128, "é" * 129),
        (
            "claude-max-1",
            "scopes",
            [f"scope-{index}" for index in range(128)],
            [f"scope-{index}" for index in range(129)],
        ),
        (
            "claude-max-1",
            "scopes",
            ["x" * 4_096],
            ["x" * 4_097],
        ),
        (
            "codex-plus-1",
            "codex_home",
            "x" * 32_768,
            "x" * 32_769,
        ),
        (
            "codex-plus-1",
            "codex_last_refresh",
            "x" * 4_096,
            "x" * 4_097,
        ),
        (
            "claude-max-1",
            "last_refresh_error",
            "x" * 4_096,
            "x" * 4_097,
        ),
        (
            "codex-plus-1",
            "heartbeat_targets",
            [f"target-{index}" for index in range(32)],
            [f"target-{index}" for index in range(33)],
        ),
        (
            "codex-plus-1",
            "heartbeat_targets",
            ["x" * 256],
            ["x" * 257],
        ),
        (
            "codex-plus-1",
            "heartbeat_window_resets",
            {f"target-{index}": EXPIRY for index in range(32)},
            {f"target-{index}": EXPIRY for index in range(33)},
        ),
        (
            "codex-plus-1",
            "heartbeat_window_resets",
            {"x" * 256: EXPIRY},
            {"x" * 257: EXPIRY},
        ),
    ],
    ids=[
        "access-token",
        "refresh-token",
        "id-token",
        "provider-account-id",
        "plan-utf8-bytes",
        "scope-count",
        "scope-bytes",
        "codex-home",
        "codex-last-refresh",
        "diagnostic",
        "target-count",
        "target-bytes",
        "reset-count",
        "reset-key-bytes",
    ],
)
def test_exact_field_bounds_accept_the_limit_and_reject_one_more(
    label: str,
    field_name: str,
    valid_value: JsonValue,
    invalid_value: JsonValue,
) -> None:
    """Every named collection and string limit is byte/count exact."""
    valid_root = _version_one_root()
    _account_record(valid_root, label)[field_name] = valid_value
    decode_version_one(_payload(valid_root))

    invalid_root = _version_one_root()
    _account_record(invalid_root, label)[field_name] = invalid_value
    with pytest.raises(InvalidSchemaError):
        decode_version_one(_payload(invalid_root))


@pytest.mark.parametrize(
    ("label", "field_name", "value"),
    [
        ("claude-max-1", "access_token", ""),
        ("codex-plus-1", "provider_account_id", ""),
        ("claude-max-1", "plan", ""),
        ("codex-plus-1", "codex_home", ""),
        ("claude-max-1", "scopes", [""]),
        ("codex-plus-1", "heartbeat_targets", [""]),
        ("codex-plus-1", "heartbeat_window_resets", {"": EXPIRY}),
    ],
)
def test_present_bounded_strings_require_at_least_one_byte(
    label: str,
    field_name: str,
    value: JsonValue,
) -> None:
    """Each distinct bounded-string family rejects an empty value."""
    root = _version_one_root()
    _account_record(root, label)[field_name] = value

    with pytest.raises(InvalidSchemaError):
        decode_version_one(_payload(root))


def test_account_and_document_limits_fail_before_state_is_authorized() -> None:
    """Account-count and complete-byte bounds cannot become valid state."""
    maximum_label = "é" * 256
    root = _version_one_root()
    accounts = root["accounts"]
    assert isinstance(accounts, dict)
    record = accounts.pop("claude-max-1")
    accounts[maximum_label] = record
    decode_version_one(_payload(root))

    accounts[maximum_label + "x"] = accounts.pop(maximum_label)
    with pytest.raises(InvalidSchemaError):
        decode_version_one(_payload(root))

    maximum_accounts: JsonObject = {
        f"account-{index}": _record(
            ProviderId.CLAUDE,
            version_one=False,
        )
        for index in range(MAX_ACCOUNTS)
    }
    decode_generation_zero(_payload(maximum_accounts))

    too_many = dict(maximum_accounts)
    too_many["one-too-many"] = _record(
        ProviderId.CLAUDE,
        version_one=False,
    )
    with pytest.raises(InvalidSchemaError):
        decode_generation_zero(_payload(too_many))

    exact_limit = _generation_zero_at_byte_limit()
    decode_generation_zero(exact_limit)
    with pytest.raises(InvalidSchemaError):
        decode_authority(exact_limit + b" ")


@pytest.mark.parametrize(
    "field_name",
    ["scopes", "heartbeat_targets"],
)
def test_set_like_lists_reject_duplicates(field_name: str) -> None:
    """Known lists never preserve contradictory duplicate metadata."""
    root = _version_one_root()
    label = "claude-max-1" if field_name == "scopes" else "codex-plus-1"
    _account_record(root, label)[field_name] = ["same", "same"]

    with pytest.raises(InvalidSchemaError):
        decode_version_one(_payload(root))


@pytest.mark.parametrize(
    ("label", "field_name", "value"),
    [
        ("claude-max-1", "provider_id", "other"),
        ("claude-max-1", "last_refresh_status", "pending"),
        ("codex-plus-1", "last_heartbeat_status", "unknown"),
        ("claude-max-1", "scopes", ["user:profile", 1]),
        ("codex-plus-1", "heartbeat_targets", ["standard", 1]),
        ("codex-plus-1", "heartbeat_window_resets", {"standard": 1}),
    ],
)
def test_closed_vocabulary_and_nested_containers_remain_strict(
    label: str,
    field_name: str,
    value: JsonValue,
) -> None:
    """Unknown tags and mixed nested values cannot enter stored state."""
    root = _version_one_root()
    _account_record(root, label)[field_name] = value

    with pytest.raises(InvalidSchemaError):
        decode_version_one(_payload(root))


def test_released_enabled_heartbeat_status_remains_valid() -> None:
    """Strict status validation retains the less-common released member."""
    root = _version_one_root()
    _account_record(root, "codex-plus-1")["last_heartbeat_status"] = "enabled"

    account = decode_version_one(_payload(root)).accounts[1]

    assert account.last_heartbeat_status is not None
    assert account.last_heartbeat_status.value == "enabled"


@pytest.mark.parametrize(
    "value",
    [
        "0001-01-01T00:00:00Z",
        "2026-07-10T12:00:00.1Z",
        "9999-12-31T23:59:59.123456+00:00",
    ],
)
def test_generation_zero_accepts_the_exact_historical_time_grammar(
    value: str,
) -> None:
    """Released UTC forms retain calendar semantics and microseconds."""
    root = _generation_zero_root()
    record = root["claude-max-1"]
    assert isinstance(record, dict)
    record["last_refresh_at"] = value

    decode_generation_zero(_payload(root))


@pytest.mark.parametrize(
    "value",
    [
        "0000-01-01T00:00:00Z",
        "2026-02-29T00:00:00Z",
        "2026-07-10 12:00:00Z",
        "2026-07-10T12:00:60Z",
        "2026-07-10T12:00:00",
        "2026-07-10T12:00:00.1234567Z",
        "2026-07-10T12:00:00+01:00",
        "\uff12\uff10\uff12\uff16-07-10T12:00:00Z",
    ],
)
def test_generation_zero_rejects_non_contract_timestamps(value: str) -> None:
    """Ambiguous, non-UTC, and semantically impossible times fail closed."""
    root = _generation_zero_root()
    record = root["claude-max-1"]
    assert isinstance(record, dict)
    record["last_refresh_at"] = value

    with pytest.raises(InvalidSchemaError):
        decode_generation_zero(_payload(root))


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-10T12:00:00Z",
        "2026-07-10T12:00:00.1Z",
        "2026-07-10T12:00:00.000000+00:00",
    ],
)
def test_version_one_requires_six_digits_and_utc_z(value: str) -> None:
    """Historically valid variants are not canonical version-one bytes."""
    root = _version_one_root()
    _account_record(root, "claude-max-1")["last_refresh_at"] = value

    with pytest.raises(InvalidSchemaError):
        decode_version_one(_payload(root))


@pytest.mark.parametrize(
    ("provider_id", "maximum", "canonical_max", "too_precise"),
    [
        (
            ProviderId.CLAUDE,
            253_402_300_799_999,
            "9999-12-31T23:59:59.999000Z",
            "2026-07-11T12:00:00.000001Z",
        ),
        (
            ProviderId.CODEX,
            253_402_300_799,
            "9999-12-31T23:59:59.000000Z",
            "2026-07-11T12:00:00.001000Z",
        ),
    ],
)
def test_provider_epoch_ranges_and_precision_are_exact(
    provider_id: ProviderId,
    maximum: int,
    canonical_max: str,
    too_precise: str,
) -> None:
    """Booleans, range overflow, and truncation never enter core time."""
    label = f"{provider_id}-account"
    generation_zero: JsonObject = {
        label: _record(provider_id, version_one=False)
    }
    record = generation_zero[label]
    assert isinstance(record, dict)
    record["expires_at"] = 0
    assert decode_generation_zero(_payload(generation_zero)).accounts[
        0
    ].expires_at == datetime(1970, 1, 1, tzinfo=UTC)
    record["expires_at"] = maximum
    decoded = decode_generation_zero(_payload(generation_zero))
    assert decoded.accounts[0].expires_at == datetime(
        9999,
        12,
        31,
        23,
        59,
        59,
        999_000 if provider_id is ProviderId.CLAUDE else 0,
        tzinfo=UTC,
    )
    assert (
        json.loads(encode_generation_zero(decoded))[label]["expires_at"]
        == maximum
    )

    for invalid in (True, 1.0, -1, maximum + 1):
        record["expires_at"] = invalid
        with pytest.raises(InvalidSchemaError):
            decode_generation_zero(_payload(generation_zero))
    record["expires_at"] = canonical_max
    with pytest.raises(InvalidSchemaError):
        decode_generation_zero(_payload(generation_zero))

    version_one = _version_one_root()
    version_label = (
        "claude-max-1" if provider_id is ProviderId.CLAUDE else "codex-plus-1"
    )
    version_record = _account_record(version_one, version_label)
    version_record["expires_at"] = canonical_max
    decode_version_one(_payload(version_one))
    version_record["expires_at"] = maximum
    with pytest.raises(InvalidSchemaError):
        decode_version_one(_payload(version_one))
    version_record["expires_at"] = too_precise
    with pytest.raises(InvalidSchemaError):
        decode_version_one(_payload(version_one))


@pytest.mark.parametrize(
    ("provider_id", "field_name", "value"),
    [
        (ProviderId.CLAUDE, "provider_account_id", "acct_test"),
        (ProviderId.CLAUDE, "codex_home", "/synthetic/codex"),
        (ProviderId.CLAUDE, "codex_id_token", "test-only-id"),
        (ProviderId.CLAUDE, "codex_last_refresh", AUDIT_TIME),
        (ProviderId.CODEX, "scopes", ["user:profile"]),
    ],
)
@pytest.mark.parametrize("version_one", [False, True])
def test_provider_incompatible_fields_fail_without_discarding_data(
    provider_id: ProviderId,
    field_name: str,
    value: JsonValue,
    *,
    version_one: bool,
) -> None:
    """Migration never nulls another provider's metadata silently."""
    label = f"{provider_id}-account"
    record = _record(provider_id, version_one=version_one)
    record[field_name] = value
    if version_one:
        root: JsonObject = {
            "schema_version": 1,
            "accounts": {label: record},
        }
        decoder = decode_version_one
    else:
        root = {label: record}
        decoder = decode_generation_zero

    with pytest.raises(InvalidSchemaError):
        decoder(_payload(root))


def test_historical_defaults_and_prototype_shape_are_closed() -> None:
    """The v0.1 base expands exactly while prototype input stays distinct."""
    minimal: JsonObject = {
        "claude-old": {
            "provider_id": "claude",
            "access_token": "test-only-token",
            "refresh_token": None,
            "expires_at": None,
            "plan": "max",
        }
    }
    account = decode_generation_zero(_payload(minimal)).accounts[0]
    assert account.scopes is None
    assert account.heartbeat_enabled is False
    assert account.heartbeat_targets is None

    prototype = decode_prototype(
        b'{"prototype":{"token":"test-only-token","plan":"max"}}'
    )
    imported = prototype_to_version_one(prototype)
    imported_record = imported.accounts[0]
    assert imported_record.provider_id is ProviderId.CLAUDE
    assert imported_record.expires_at is None
    assert imported_record.heartbeat_enabled is False

    for invalid in (
        b'{"prototype":{"token":"test-only-token"}}',
        b'{"prototype":{"token":"test-only-token","plan":"max","extra":true}}',
        b'{"prototype":{"access_token":"test-only-token","plan":"max"}}',
    ):
        with pytest.raises(InvalidSchemaError):
            decode_prototype(invalid)


@pytest.mark.parametrize(
    "case",
    ["missing-base", "unknown-field", "string-boolean", "non-object"],
)
def test_generation_zero_is_strict_across_historical_defaults(
    case: str,
) -> None:
    """Defaults never authorize absent base data, coercion, or extras."""
    root: JsonObject = {
        "claude-old": {
            "provider_id": "claude",
            "access_token": "test-only-token",
            "refresh_token": None,
            "expires_at": None,
            "plan": "max",
        }
    }
    record = root["claude-old"]
    assert isinstance(record, dict)
    if case == "missing-base":
        record.pop("refresh_token")
    elif case == "unknown-field":
        record["extra"] = True
    elif case == "string-boolean":
        record["heartbeat_enabled"] = "false"
    else:
        root["claude-old"] = "not-an-object"

    with pytest.raises(InvalidSchemaError):
        decode_generation_zero(_payload(root))


def test_prototype_receipt_has_one_exact_non_secret_encoding() -> None:
    """Receipt bytes and hash grammar are deterministic and closed."""
    digest = "0123456789abcdef" * 4
    expected = (
        "{\n"
        '  "receipt_version": 1,\n'
        f'  "prototype_sha256": "{digest}",\n'
        '  "target_schema_version": 2\n'
        "}\n"
    ).encode()

    encoded = encode_prototype_receipt(PrototypeReceipt(digest))

    assert encoded == expected
    assert decode_prototype_receipt(encoded) == PrototypeReceipt(digest)
    for invalid in (
        expected.replace(b'"receipt_version": 1', b'"receipt_version": 2'),
        expected.replace(digest.encode(), digest.upper().encode()),
        expected.replace(digest.encode(), digest[:-1].encode()),
        expected.replace(
            b'"target_schema_version": 2',
            b'"target_schema_version": 3',
        ),
        expected.replace(b"\n}", b',\n  "extra": true\n}'),
        _payload(
            {
                "prototype_sha256": digest,
                "receipt_version": 1,
                "target_schema_version": 2,
            }
        ),
    ):
        with pytest.raises(InvalidSchemaError):
            decode_prototype_receipt(invalid)

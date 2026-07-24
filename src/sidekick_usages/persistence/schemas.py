"""Strict account schemas and deterministic persistence codecs."""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum, auto

from pydantic import TypeAdapter, ValidationError

from sidekick_usages.core.types import (
    AccountLabel,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.persistence._current_schema import (
    decode_current,
    encode_current,
)
from sidekick_usages.persistence._prototype_receipt_schema import (
    decode_receipt,
    encode_receipt,
)
from sidekick_usages.persistence._schema_models import (
    GENERATION_ZERO_ADAPTER,
    PROTOTYPE_ADAPTER,
    VERSION_ONE_ADAPTER,
    ValidatedAccountRecord,
)
from sidekick_usages.persistence.errors import (
    DuplicateKeyError,
    FutureSchemaError,
    InvalidSchemaError,
    MalformedJsonError,
    PersistenceSchemaError,
    SchemaIssue,
    SchemaIssueCode,
)
from sidekick_usages.persistence.limits import (
    MAX_ACCOUNTS,
    MAX_DOCUMENT_BYTES,
)
from sidekick_usages.persistence.time_codec import (
    canonical_timestamp,
    parse_canonical_timestamp,
)
from sidekick_usages.serialization import (
    JsonDecodeCode,
    JsonDecodeError,
    JsonObject,
    JsonValue,
    decode_json_value,
)

CURRENT_SCHEMA_VERSION = 2

_MIN_HISTORICAL_TIMESTAMP_LENGTH = 20
_MAX_HISTORICAL_TIMESTAMP_LENGTH = 32

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECONDS_PER_SECOND = 1_000_000
_MICROSECONDS_PER_MILLISECOND = 1_000
_MAX_CLAUDE_EXPIRY = 253_402_300_799_999
_MAX_CODEX_EXPIRY = 253_402_300_799

_HISTORICAL_TIMESTAMP = re.compile(
    r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-"
    r"(?P<day>[0-9]{2})T(?P<hour>[0-9]{2}):"
    r"(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,6}))?(?:Z|\+00:00)\Z",
    re.ASCII,
)
_RECORD_FIELDS = (
    "provider_id",
    "provider_account_id",
    "access_token",
    "refresh_token",
    "expires_at",
    "plan",
    "scopes",
    "codex_home",
    "codex_id_token",
    "codex_last_refresh",
    "last_refresh_at",
    "last_refresh_status",
    "last_refresh_error",
    "heartbeat_enabled",
    "heartbeat_5h_reset_at",
    "heartbeat_window_resets",
    "heartbeat_targets",
    "last_heartbeat_at",
    "last_heartbeat_status",
    "last_heartbeat_error",
)
_RECORD_FIELD_SET = frozenset(_RECORD_FIELDS)
_SAFE_SCHEMA_PATH_SEGMENTS = frozenset(
    {
        *_RECORD_FIELDS,
        "accounts",
        "claude",
        "claude_identity",
        "codex",
        "credential_kind",
        "access_expires_at",
        "refresh_expires_at",
        "account_id",
        "organization_id",
        "prototype_sha256",
        "receipt_version",
        "schema_version",
        "target_schema_version",
        "token",
    }
)
_GENERATION_ZERO_REQUIRED = frozenset(
    {
        "provider_id",
        "access_token",
        "refresh_token",
        "expires_at",
        "plan",
    }
)
_GENERATION_ZERO_NULL_FIELDS = (
    "provider_account_id",
    "scopes",
    "codex_home",
    "codex_id_token",
    "codex_last_refresh",
    "last_refresh_at",
    "last_refresh_status",
    "last_refresh_error",
    "heartbeat_5h_reset_at",
    "heartbeat_window_resets",
    "heartbeat_targets",
    "last_heartbeat_at",
    "last_heartbeat_status",
    "last_heartbeat_error",
)
_GENERATION_ZERO_DEFAULTS: JsonObject = {
    **dict.fromkeys(_GENERATION_ZERO_NULL_FIELDS),
    "heartbeat_enabled": False,
}


@dataclass(frozen=True, slots=True)
class StoredAccountRecord:
    """One normalized flattened account at the persistence boundary."""

    label: AccountLabel
    provider_id: ProviderId
    provider_account_id: str | None
    access_token: str = field(repr=False)
    refresh_token: str | None = field(repr=False)
    expires_at: datetime | None
    plan: str
    scopes: tuple[str, ...] | None
    codex_home: str | None
    codex_id_token: str | None = field(repr=False)
    codex_last_refresh: str | None
    last_refresh_at: datetime | None
    last_refresh_status: RefreshStatus | None
    last_refresh_error: str | None
    heartbeat_enabled: bool
    heartbeat_5h_reset_at: datetime | None
    heartbeat_window_resets: tuple[tuple[str, datetime], ...] | None
    heartbeat_targets: tuple[str, ...] | None
    last_heartbeat_at: datetime | None
    last_heartbeat_status: HeartbeatStatus | None
    last_heartbeat_error: str | None
    credential_kind: ClaudeCredentialKind | None = None
    refresh_expires_at: datetime | None = None
    claude_identity: StoredClaudeIdentity | None = field(
        default=None,
        repr=False,
    )


class ClaudeCredentialKind(StrEnum):
    """Closed persisted Claude credential variants."""

    SETUP_TOKEN = auto()
    SUBSCRIPTION_LOGIN = "subscription_login"


@dataclass(frozen=True, slots=True)
class StoredClaudeIdentity:
    """Complete stable Claude identity persisted as one value."""

    account_id: str = field(repr=False)
    organization_id: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PrototypeAccount:
    """One strict import-only prototype account."""

    label: AccountLabel
    token: str = field(repr=False)
    plan: str


@dataclass(frozen=True, slots=True)
class PrototypeDocument:
    """Validated prototype accounts in source insertion order."""

    accounts: tuple[PrototypeAccount, ...]


@dataclass(frozen=True, slots=True)
class PrototypeReceipt:
    """Non-secret proof that one exact prototype was imported."""

    prototype_sha256: str
    target_schema_version: int = 2


@dataclass(frozen=True, slots=True)
class GenerationZeroDocument:
    """Validated released unversioned accounts in insertion order."""

    accounts: tuple[StoredAccountRecord, ...]


@dataclass(frozen=True, slots=True)
class VersionOneDocument:
    """Validated schema-version-one accounts in insertion order."""

    accounts: tuple[StoredAccountRecord, ...]


@dataclass(frozen=True, slots=True)
class VersionTwoDocument:
    """Validated current schema-version-two accounts in insertion order."""

    accounts: tuple[StoredAccountRecord, ...]


type StoredDocument = (
    GenerationZeroDocument | VersionOneDocument | VersionTwoDocument
)


def _decode_json(payload: bytes) -> JsonValue:
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise InvalidSchemaError
    try:
        decoded = decode_json_value(payload)
    except JsonDecodeError as decode_error:
        code = decode_error.code
    else:
        return decoded
    error: PersistenceSchemaError
    if code is JsonDecodeCode.DUPLICATE_KEY:
        error = DuplicateKeyError()
    else:
        error = MalformedJsonError()
    raise error


def _object_root(payload: bytes) -> JsonObject:
    decoded = _decode_json(payload)
    if not isinstance(decoded, dict):
        raise InvalidSchemaError
    return decoded


def _schema_issue_code(pydantic_code: str) -> SchemaIssueCode:
    if pydantic_code in {"missing", "union_tag_not_found"}:
        return SchemaIssueCode.MISSING_FIELD
    if pydantic_code == "extra_forbidden":
        return SchemaIssueCode.UNEXPECTED_FIELD
    if pydantic_code.endswith("_type"):
        return SchemaIssueCode.INVALID_TYPE
    return SchemaIssueCode.INVALID_VALUE


def _schema_issue_message(code: SchemaIssueCode) -> str:
    if code is SchemaIssueCode.MISSING_FIELD:
        return "Required field is missing."
    if code is SchemaIssueCode.UNEXPECTED_FIELD:
        return "Field is not supported by this schema."
    if code is SchemaIssueCode.INVALID_TYPE:
        return "Field has the wrong JSON type."
    return "Field value violates the schema contract."


def _safe_schema_path(
    path: tuple[str | int, ...],
) -> tuple[str | int, ...]:
    return tuple(
        segment
        if isinstance(segment, int) or segment in _SAFE_SCHEMA_PATH_SEGMENTS
        else "<key>"
        for segment in path
    )


def _project_validation_error(
    error: ValidationError,
) -> tuple[SchemaIssue, ...]:
    details = error.errors(include_input=False, include_url=False)
    issues: list[SchemaIssue] = []
    for detail in details:
        code = _schema_issue_code(detail["type"])
        issues.append(
            SchemaIssue(
                path=_safe_schema_path(detail["loc"]),
                code=code,
                message=_schema_issue_message(code),
            )
        )
    return tuple(issues)


def _validate[T](adapter: TypeAdapter[T], value: object) -> T:
    try:
        result = adapter.validate_python(value, strict=True)
    except ValidationError as validation_error:
        error = InvalidSchemaError(
            _project_validation_error(validation_error),
        )
    else:
        return result
    raise error


def _validated_label(value: str) -> AccountLabel:
    try:
        label = AccountLabel(value)
    except ValueError:
        error = InvalidSchemaError()
    else:
        return label
    raise error


def _validate_account_keys(accounts: JsonObject) -> None:
    if len(accounts) > MAX_ACCOUNTS:
        raise InvalidSchemaError
    for label in accounts:
        _validated_label(label)


def _timestamp_from_match(match: re.Match[str]) -> datetime:
    fraction = match.group("fraction") or ""
    microsecond = int(fraction.ljust(6, "0")) if fraction else 0
    try:
        result = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            microsecond,
            tzinfo=UTC,
        )
    except ValueError:
        error = InvalidSchemaError()
    else:
        return result
    raise error


def _parse_historical_timestamp(value: str) -> datetime:
    if not value.isascii() or not (
        _MIN_HISTORICAL_TIMESTAMP_LENGTH
        <= len(value)
        <= _MAX_HISTORICAL_TIMESTAMP_LENGTH
    ):
        raise InvalidSchemaError
    match = _HISTORICAL_TIMESTAMP.fullmatch(value)
    if match is None:
        raise InvalidSchemaError
    return _timestamp_from_match(match)


def _native_expiry(provider_id: ProviderId, value: int) -> datetime:
    maximum = (
        _MAX_CLAUDE_EXPIRY
        if provider_id is ProviderId.CLAUDE
        else _MAX_CODEX_EXPIRY
    )
    if value < 0 or value > maximum:
        raise InvalidSchemaError
    delta = (
        timedelta(milliseconds=value)
        if provider_id is ProviderId.CLAUDE
        else timedelta(seconds=value)
    )
    return _EPOCH + delta


def _expiry_native_value(
    provider_id: ProviderId,
    value: datetime,
) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise InvalidSchemaError
    delta = value.astimezone(UTC) - _EPOCH
    microseconds = (
        delta.days * 86_400 + delta.seconds
    ) * _MICROSECONDS_PER_SECOND + delta.microseconds
    unit = (
        _MICROSECONDS_PER_MILLISECOND
        if provider_id is ProviderId.CLAUDE
        else _MICROSECONDS_PER_SECOND
    )
    if microseconds < 0 or microseconds % unit:
        raise InvalidSchemaError
    result = microseconds // unit
    maximum = (
        _MAX_CLAUDE_EXPIRY
        if provider_id is ProviderId.CLAUDE
        else _MAX_CODEX_EXPIRY
    )
    if result > maximum:
        raise InvalidSchemaError
    return result


def _stored_timestamp(
    value: str | None,
    *,
    canonical: bool,
) -> datetime | None:
    if value is None:
        return None
    if canonical:
        return parse_canonical_timestamp(value)
    return _parse_historical_timestamp(value)


def _record_from_model(
    label_value: str,
    model: ValidatedAccountRecord,
    *,
    version_one: bool,
) -> StoredAccountRecord:
    provider_id = ProviderId(model.provider_id)
    expiry_value = model.expires_at
    if expiry_value is None:
        expires_at = None
    elif version_one and isinstance(expiry_value, str):
        expires_at = parse_canonical_timestamp(expiry_value)
        _expiry_native_value(provider_id, expires_at)
    elif not version_one and type(expiry_value) is int:
        expires_at = _native_expiry(provider_id, expiry_value)
    else:
        raise InvalidSchemaError
    timestamp_parser = (
        parse_canonical_timestamp
        if version_one
        else _parse_historical_timestamp
    )
    resets = (
        tuple(
            (target_id, timestamp_parser(reset_at))
            for target_id, reset_at in model.heartbeat_window_resets.items()
        )
        if model.heartbeat_window_resets is not None
        else None
    )
    return StoredAccountRecord(
        label=_validated_label(label_value),
        provider_id=provider_id,
        provider_account_id=model.provider_account_id,
        access_token=model.access_token,
        refresh_token=model.refresh_token,
        expires_at=expires_at,
        plan=model.plan,
        scopes=tuple(model.scopes) if model.scopes is not None else None,
        codex_home=model.codex_home,
        codex_id_token=model.codex_id_token,
        codex_last_refresh=model.codex_last_refresh,
        last_refresh_at=_stored_timestamp(
            model.last_refresh_at,
            canonical=version_one,
        ),
        last_refresh_status=(
            RefreshStatus(model.last_refresh_status)
            if model.last_refresh_status is not None
            else None
        ),
        last_refresh_error=model.last_refresh_error,
        heartbeat_enabled=model.heartbeat_enabled,
        heartbeat_5h_reset_at=_stored_timestamp(
            model.heartbeat_5h_reset_at,
            canonical=version_one,
        ),
        heartbeat_window_resets=resets,
        heartbeat_targets=(
            tuple(model.heartbeat_targets)
            if model.heartbeat_targets is not None
            else None
        ),
        last_heartbeat_at=_stored_timestamp(
            model.last_heartbeat_at,
            canonical=version_one,
        ),
        last_heartbeat_status=(
            HeartbeatStatus(model.last_heartbeat_status)
            if model.last_heartbeat_status is not None
            else None
        ),
        last_heartbeat_error=model.last_heartbeat_error,
    )


def _generation_zero_input(root: JsonObject) -> JsonObject:
    _validate_account_keys(root)
    prepared: JsonObject = {}
    for label, raw_record in root.items():
        if not isinstance(raw_record, dict):
            raise InvalidSchemaError
        keys = frozenset(raw_record)
        if (
            not keys >= _GENERATION_ZERO_REQUIRED
            or not keys <= _RECORD_FIELD_SET
        ):
            raise InvalidSchemaError
        prepared[label] = {
            field_name: (
                raw_record[field_name]
                if field_name in raw_record
                else _GENERATION_ZERO_DEFAULTS[field_name]
            )
            for field_name in _RECORD_FIELDS
        }
    return prepared


def _generation_zero_from_root(root: JsonObject) -> GenerationZeroDocument:
    models = _validate(
        GENERATION_ZERO_ADAPTER,
        _generation_zero_input(root),
    )
    return GenerationZeroDocument(
        tuple(
            _record_from_model(label, model, version_one=False)
            for label, model in models.items()
        )
    )


def _version_one_from_root(root: JsonObject) -> VersionOneDocument:
    accounts_value = root.get("accounts")
    if isinstance(accounts_value, dict):
        _validate_account_keys(accounts_value)
    envelope = _validate(VERSION_ONE_ADAPTER, root)
    return VersionOneDocument(
        tuple(
            _record_from_model(label, model, version_one=True)
            for label, model in envelope.accounts.items()
        )
    )


def decode_authority(payload: bytes) -> StoredDocument:
    """Decode an authoritative account document by strict root dispatch.

    :param payload: Complete bounded authority bytes.
    :returns: Validated generation-zero, version-one, or version-two document.
    :raises PersistenceSchemaError: If the bytes are not supported state.
    """
    root = _object_root(payload)
    schema_version = root.get("schema_version")
    if type(schema_version) is int:
        if schema_version == CURRENT_SCHEMA_VERSION:
            return decode_version_two(payload)
        if schema_version != 1:
            raise FutureSchemaError(schema_version)
        return _version_one_from_root(root)
    return _generation_zero_from_root(root)


def decode_generation_zero(payload: bytes) -> GenerationZeroDocument:
    """Decode one strict released unversioned Sidekick document."""
    root = _object_root(payload)
    if type(root.get("schema_version")) is int:
        raise InvalidSchemaError
    return _generation_zero_from_root(root)


def decode_version_one(payload: bytes) -> VersionOneDocument:
    """Decode one strict schema-version-one Sidekick document."""
    root = _object_root(payload)
    schema_version = root.get("schema_version")
    if type(schema_version) is int and schema_version != 1:
        raise FutureSchemaError(schema_version)
    if schema_version != 1 or type(schema_version) is not int:
        raise InvalidSchemaError
    return _version_one_from_root(root)


def decode_version_two(payload: bytes) -> VersionTwoDocument:
    """Decode one strict current schema-version-two document."""
    return decode_current(payload)


def decode_prototype(payload: bytes) -> PrototypeDocument:
    """Decode the strict import-only cc-usage prototype shape."""
    root = _object_root(payload)
    _validate_account_keys(root)
    models = _validate(PROTOTYPE_ADAPTER, root)
    return PrototypeDocument(
        tuple(
            PrototypeAccount(
                label=_validated_label(label),
                token=model.token,
                plan=model.plan,
            )
            for label, model in models.items()
        )
    )


def decode_prototype_receipt(payload: bytes) -> PrototypeReceipt:
    """Decode one exact deterministic prototype-import receipt."""
    return decode_receipt(payload)


def _reset_object(
    resets: tuple[tuple[str, datetime], ...] | None,
) -> JsonObject | None:
    if resets is None:
        return None
    result: JsonObject = {}
    for target_id, reset_at in resets:
        if target_id in result:
            raise InvalidSchemaError
        result[target_id] = canonical_timestamp(reset_at)
    return result


def _record_object(
    record: StoredAccountRecord,
    *,
    version_one: bool,
) -> JsonObject:
    if record.expires_at is None:
        expires_at: JsonValue = None
    elif version_one:
        _expiry_native_value(record.provider_id, record.expires_at)
        expires_at = canonical_timestamp(record.expires_at)
    else:
        expires_at = _expiry_native_value(
            record.provider_id,
            record.expires_at,
        )
    return {
        "provider_id": record.provider_id.value,
        "provider_account_id": record.provider_account_id,
        "access_token": record.access_token,
        "refresh_token": record.refresh_token,
        "expires_at": expires_at,
        "plan": record.plan,
        "scopes": list(record.scopes) if record.scopes is not None else None,
        "codex_home": record.codex_home,
        "codex_id_token": record.codex_id_token,
        "codex_last_refresh": record.codex_last_refresh,
        "last_refresh_at": (
            canonical_timestamp(record.last_refresh_at)
            if record.last_refresh_at is not None
            else None
        ),
        "last_refresh_status": (
            record.last_refresh_status.value
            if record.last_refresh_status is not None
            else None
        ),
        "last_refresh_error": record.last_refresh_error,
        "heartbeat_enabled": record.heartbeat_enabled,
        "heartbeat_5h_reset_at": (
            canonical_timestamp(record.heartbeat_5h_reset_at)
            if record.heartbeat_5h_reset_at is not None
            else None
        ),
        "heartbeat_window_resets": _reset_object(
            record.heartbeat_window_resets
        ),
        "heartbeat_targets": (
            list(record.heartbeat_targets)
            if record.heartbeat_targets is not None
            else None
        ),
        "last_heartbeat_at": (
            canonical_timestamp(record.last_heartbeat_at)
            if record.last_heartbeat_at is not None
            else None
        ),
        "last_heartbeat_status": (
            record.last_heartbeat_status.value
            if record.last_heartbeat_status is not None
            else None
        ),
        "last_heartbeat_error": record.last_heartbeat_error,
    }


def _accounts_object(
    records: tuple[StoredAccountRecord, ...],
    *,
    version_one: bool,
) -> JsonObject:
    if len(records) > MAX_ACCOUNTS:
        raise InvalidSchemaError
    result: JsonObject = {}
    for record in records:
        label = _validated_label(str(record.label))
        if label in result:
            raise InvalidSchemaError
        result[label] = _record_object(record, version_one=version_one)
    return result


def _encode_json(root: JsonObject) -> bytes:
    try:
        payload = (
            json.dumps(
                root,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except TypeError, ValueError, UnicodeEncodeError:
        error = InvalidSchemaError()
    else:
        if len(payload) <= MAX_DOCUMENT_BYTES:
            return payload
        error = InvalidSchemaError()
    raise error


def encode_version_one(document: VersionOneDocument) -> bytes:
    """Encode deterministic, canonical schema-version-one bytes."""
    root: JsonObject = {
        "schema_version": 1,
        "accounts": _accounts_object(
            document.accounts,
            version_one=True,
        ),
    }
    payload = _encode_json(root)
    decode_version_one(payload)
    return payload


def encode_version_two(document: VersionTwoDocument) -> bytes:
    """Encode deterministic current schema-version-two bytes."""
    return encode_current(document)


def encode_generation_zero(document: GenerationZeroDocument) -> bytes:
    """Encode the complete deterministic v0.6.0 generation-zero shape."""
    payload = _encode_json(
        _accounts_object(document.accounts, version_one=False)
    )
    decode_generation_zero(payload)
    return payload


def encode_prototype_receipt(receipt: PrototypeReceipt) -> bytes:
    """Encode the exact deterministic non-secret receipt format."""
    return encode_receipt(receipt)

"""Strict codec for authoritative account token-activity snapshots."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from sidekick_usages.core.models import (
    AccountTokenActivitySnapshot,
    TokenActivitySummary,
)
from sidekick_usages.core.types import ProviderId, TokenActivityScope
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.schema.validation import sha256_text
from sidekick_usages.persistence.state.fields import (
    require_exact_keys,
    require_integer,
    require_object,
    require_optional_string,
    require_schema_version,
    require_string,
)
from sidekick_usages.persistence.time_codec import (
    canonical_timestamp,
    canonical_timestamp_text,
    parse_canonical_timestamp,
)
from sidekick_usages.serialization.json import (
    JsonDecodeError,
    JsonEncodeError,
    JsonObject,
    JsonValue,
    decode_json_value,
    encode_compact_json,
)

ACTIVITY_SCHEMA_VERSION = 1
MAX_ACTIVITY_RECORDS = 4_096
MAX_TOKEN_COUNT = 9_223_372_036_854_775_807

_DOCUMENT_KEYS = frozenset({"accounts", "schema_version"})
_RECORD_KEYS = frozenset(
    {
        "fetched_at",
        "provider_id",
        "since",
        "total_tokens",
    }
)


class ActivitySnapshotDecodeError(ValueError):
    """Persisted activity bytes violate the current strict schema."""


def _date_text(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError from None
    if parsed.isoformat() != value:
        raise ValueError
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivitySnapshotRecord:
    """Strict persisted snapshot record."""

    provider_id: str
    total_tokens: int
    since: str | None
    fetched_at: str

    def __post_init__(self) -> None:
        """Validate one exact Codex activity record."""
        if self.provider_id != ProviderId.CODEX.value:
            raise ValueError
        if (
            type(self.total_tokens) is not int
            or self.total_tokens < 0
            or self.total_tokens > MAX_TOKEN_COUNT
        ):
            raise ValueError
        if self.since is not None:
            _date_text(self.since)
        canonical_timestamp_text(self.fetched_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class ActivitySnapshotDocument:
    """Strict versioned activity-snapshot document."""

    schema_version: int
    accounts: Mapping[str, ActivitySnapshotRecord]

    def __post_init__(self) -> None:
        """Validate bounded canonical record identities."""
        if type(self.schema_version) is not int or (
            self.schema_version != ACTIVITY_SCHEMA_VERSION
        ):
            raise ValueError
        if not isinstance(self.accounts, Mapping):
            raise TypeError
        accounts = dict(self.accounts)
        if len(accounts) > MAX_ACTIVITY_RECORDS:
            raise ValueError
        for digest, record in accounts.items():
            sha256_text(digest)
            if not isinstance(record, ActivitySnapshotRecord):
                raise TypeError
        object.__setattr__(
            self,
            "accounts",
            MappingProxyType(accounts),
        )


def _record(value: JsonValue) -> ActivitySnapshotRecord:
    record = require_object(value)
    require_exact_keys(record, _RECORD_KEYS)
    return ActivitySnapshotRecord(
        provider_id=require_string(record["provider_id"]),
        total_tokens=require_integer(record["total_tokens"]),
        since=require_optional_string(record["since"]),
        fetched_at=require_string(record["fetched_at"]),
    )


def _record_object(record: ActivitySnapshotRecord) -> JsonObject:
    return {
        "fetched_at": record.fetched_at,
        "provider_id": record.provider_id,
        "since": record.since,
        "total_tokens": record.total_tokens,
    }


def encode_activity_snapshot_document(
    document: ActivitySnapshotDocument,
) -> bytes:
    """Encode one canonical activity-snapshot document."""
    accounts: JsonObject = {
        digest: _record_object(record)
        for digest, record in document.accounts.items()
    }
    root: JsonObject = {
        "accounts": accounts,
        "schema_version": document.schema_version,
    }
    return encode_compact_json(root) + b"\n"


def decode_activity_snapshot_document(
    payload: bytes,
) -> ActivitySnapshotDocument:
    """Decode one canonical activity-snapshot document."""
    try:
        root = require_object(decode_json_value(payload))
        require_exact_keys(root, _DOCUMENT_KEYS)
        require_schema_version(
            root["schema_version"],
            ACTIVITY_SCHEMA_VERSION,
        )
        records = require_object(root["accounts"])
        if len(records) > MAX_ACTIVITY_RECORDS:
            raise InvalidSchemaError
        document = ActivitySnapshotDocument(
            schema_version=ACTIVITY_SCHEMA_VERSION,
            accounts={
                sha256_text(digest): _record(value)
                for digest, value in records.items()
            },
        )
        if encode_activity_snapshot_document(document) != payload:
            raise ActivitySnapshotDecodeError
    except (
        ActivitySnapshotDecodeError,
        InvalidSchemaError,
        JsonDecodeError,
        JsonEncodeError,
        TypeError,
        ValueError,
    ):
        raise ActivitySnapshotDecodeError from None
    return document


def activity_record(
    snapshot: AccountTokenActivitySnapshot,
) -> ActivitySnapshotRecord:
    """Convert one core snapshot to its strict persisted record."""
    if snapshot.provider_id is not ProviderId.CODEX:
        raise ValueError("Only Codex exposes account activity snapshots.")
    return ActivitySnapshotRecord(
        provider_id=ProviderId.CODEX.value,
        total_tokens=snapshot.summary.total_tokens,
        since=(
            None
            if snapshot.summary.since is None
            else snapshot.summary.since.isoformat()
        ),
        fetched_at=canonical_timestamp(snapshot.fetched_at),
    )


def account_activity_snapshot(
    provider_account_id: str,
    record: ActivitySnapshotRecord,
) -> AccountTokenActivitySnapshot:
    """Convert one strict record to a core account snapshot."""
    return AccountTokenActivitySnapshot(
        provider_id=ProviderId(record.provider_id),
        provider_account_id=provider_account_id,
        summary=TokenActivitySummary(
            total_tokens=record.total_tokens,
            scope=TokenActivityScope.ACCOUNT,
            since=(
                None
                if record.since is None
                else date.fromisoformat(record.since)
            ),
        ),
        fetched_at=parse_canonical_timestamp(record.fetched_at),
    )

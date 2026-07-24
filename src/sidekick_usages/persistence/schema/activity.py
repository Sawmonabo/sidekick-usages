"""Strict codec for authoritative account token-activity snapshots."""

import json
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from sidekick_usages.core.models import (
    AccountTokenActivitySnapshot,
    TokenActivitySummary,
)
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import ProviderId, TokenActivityScope
from sidekick_usages.persistence.types.artifact import Sha256Digest
from sidekick_usages.serialization import JsonDecodeError, decode_json_value

ACTIVITY_SCHEMA_VERSION = 1
MAX_ACTIVITY_RECORDS = 4_096
MAX_TOKEN_COUNT = 9_223_372_036_854_775_807
MODEL_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)


class ActivitySnapshotDecodeError(ValueError):
    """Persisted activity bytes violate the current strict schema."""


def _digest(value: str) -> str:
    Sha256Digest(value)
    return value


def _date_text(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError from None
    if parsed.isoformat() != value:
        raise ValueError
    return value


def _timestamp(value: datetime) -> str:
    utc_value = as_utc(value)
    return utc_value.isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _timestamp_text(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        canonical = _timestamp(parsed)
    except ValueError:
        raise ValueError from None
    if canonical != value:
        raise ValueError
    return value


def _records(
    value: dict[str, ActivitySnapshotRecord],
) -> dict[str, ActivitySnapshotRecord]:
    if len(value) > MAX_ACTIVITY_RECORDS:
        raise ValueError
    return value


type DigestText = Annotated[str, AfterValidator(_digest)]
type DateText = Annotated[str, AfterValidator(_date_text)]
type TimestampText = Annotated[str, AfterValidator(_timestamp_text)]


class ActivitySnapshotRecord(BaseModel):
    """Strict persisted snapshot record."""

    model_config = MODEL_CONFIG

    provider_id: Literal["codex"]
    total_tokens: int = Field(ge=0, le=MAX_TOKEN_COUNT)
    since: DateText | None
    fetched_at: TimestampText


type SnapshotRecords = Annotated[
    dict[DigestText, ActivitySnapshotRecord],
    AfterValidator(_records),
]


class ActivitySnapshotDocument(BaseModel):
    """Strict versioned activity-snapshot document."""

    model_config = MODEL_CONFIG

    schema_version: Literal[1]
    accounts: SnapshotRecords


def encode_activity_snapshot_document(
    document: ActivitySnapshotDocument,
) -> bytes:
    """Encode one canonical activity-snapshot document."""
    root = document.model_dump(mode="python")
    return (
        json.dumps(
            root,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def decode_activity_snapshot_document(
    payload: bytes,
) -> ActivitySnapshotDocument:
    """Decode one canonical activity-snapshot document."""
    try:
        root = decode_json_value(payload)
        document = ActivitySnapshotDocument.model_validate(
            root,
            strict=True,
        )
    except JsonDecodeError, ValidationError:
        raise ActivitySnapshotDecodeError from None
    if encode_activity_snapshot_document(document) != payload:
        raise ActivitySnapshotDecodeError
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
        fetched_at=_timestamp(snapshot.fetched_at),
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
        fetched_at=datetime.fromisoformat(
            record.fetched_at.replace("Z", "+00:00")
        ).astimezone(UTC),
    )

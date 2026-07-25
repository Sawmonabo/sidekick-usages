"""Strict codec for last-successful account usage snapshots."""

import json
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
)

from sidekick_usages.core.accounts.types import (
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    AccountUsageSnapshot,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.time_codec import (
    canonical_timestamp,
    parse_canonical_timestamp,
)
from sidekick_usages.serialization.json import (
    JsonDecodeError,
    decode_json_value,
)

USAGE_SCHEMA_VERSION = 1
MAX_USAGE_RECORDS = 4_096
MAX_USAGE_WINDOWS = 64
MAX_USAGE_TEXT_BYTES = 4_096
MODEL_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)

type AccountIdText = Annotated[str, AfterValidator(_account_id)]
type BoundedText = Annotated[str, AfterValidator(_bounded_text)]
type TimestampText = Annotated[str, AfterValidator(_timestamp_text)]
type UsageRecords = Annotated[
    dict[AccountIdText, UsageSnapshotRecord],
    AfterValidator(_records),
]
type UsageWindows = Annotated[
    list[UsageWindowRecord],
    AfterValidator(_windows),
]


class UsageSnapshotDecodeError(ValueError):
    """Persisted usage bytes violate the current strict schema."""


def _account_id(value: str) -> str:
    SidekickAccountId(value)
    return value


def _bounded_text(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError from None
    if not encoded or len(encoded) > MAX_USAGE_TEXT_BYTES:
        raise ValueError
    return value


def _timestamp_text(value: str) -> str:
    if canonical_timestamp(parse_canonical_timestamp(value)) != value:
        raise ValueError
    return value


def _records(
    value: dict[str, UsageSnapshotRecord],
) -> dict[str, UsageSnapshotRecord]:
    if len(value) > MAX_USAGE_RECORDS:
        raise ValueError
    return value


def _windows(value: list[UsageWindowRecord]) -> list[UsageWindowRecord]:
    if len(value) > MAX_USAGE_WINDOWS:
        raise ValueError
    names = tuple(window.name for window in value)
    if len(names) != len(set(names)):
        raise ValueError
    return value


class UsageWindowRecord(BaseModel):
    """Strict normalized usage-window record."""

    model_config = MODEL_CONFIG

    name: BoundedText
    utilization: float = Field(ge=0.0, le=100.0, allow_inf_nan=False)
    resets_at: TimestampText | None


class UsageSnapshotRecord(BaseModel):
    """Strict last-successful account usage record."""

    model_config = MODEL_CONFIG

    provider_id: Literal["claude", "codex"]
    provider_identity: BoundedText | None
    plan: BoundedText
    windows: UsageWindows
    fetched_at: TimestampText


class UsageSnapshotDocument(BaseModel):
    """Strict versioned account-usage document."""

    model_config = MODEL_CONFIG

    schema_version: Literal[1]
    accounts: UsageRecords


def encode_usage_snapshot_document(
    document: UsageSnapshotDocument,
) -> bytes:
    """Encode one canonical account-usage document."""
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


def decode_usage_snapshot_document(
    payload: bytes,
) -> UsageSnapshotDocument:
    """Decode one canonical account-usage document."""
    try:
        root = decode_json_value(payload)
        document = UsageSnapshotDocument.model_validate(root, strict=True)
    except JsonDecodeError, ValidationError, ValueError:
        raise UsageSnapshotDecodeError from None
    if encode_usage_snapshot_document(document) != payload:
        raise UsageSnapshotDecodeError
    return document


def usage_record(snapshot: AccountUsageSnapshot) -> UsageSnapshotRecord:
    """Convert one core snapshot to its strict persisted record."""
    return UsageSnapshotRecord(
        provider_id=snapshot.provider_id.value,
        provider_identity=(
            None
            if snapshot.provider_identity is None
            else str(snapshot.provider_identity)
        ),
        plan=snapshot.plan,
        windows=[
            UsageWindowRecord(
                name=window.name,
                utilization=window.utilization,
                resets_at=(
                    None
                    if window.resets_at is None
                    else canonical_timestamp(window.resets_at)
                ),
            )
            for window in snapshot.report.windows
        ],
        fetched_at=canonical_timestamp(snapshot.fetched_at),
    )


def account_usage_snapshot(
    account_id: SidekickAccountId,
    record: UsageSnapshotRecord,
) -> AccountUsageSnapshot:
    """Convert one strict record to a core account snapshot."""
    return AccountUsageSnapshot(
        account_id=account_id,
        provider_id=ProviderId(record.provider_id),
        provider_identity=(
            None
            if record.provider_identity is None
            else ProviderIdentity(record.provider_identity)
        ),
        plan=record.plan,
        report=UsageReport(
            windows=tuple(
                UsageWindow(
                    window.name,
                    window.utilization,
                    (
                        None
                        if window.resets_at is None
                        else parse_canonical_timestamp(window.resets_at)
                    ),
                )
                for window in record.windows
            ),
            plan=record.plan,
        ),
        fetched_at=parse_canonical_timestamp(record.fetched_at),
    )

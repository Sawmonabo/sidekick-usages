"""Strict codec for last-successful account usage snapshots."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

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
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.schema.validation import (
    bounded_text,
    canonical_account_id_text,
)
from sidekick_usages.persistence.state.fields import (
    require_exact_keys,
    require_list,
    require_number,
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

USAGE_SCHEMA_VERSION = 2
MAX_USAGE_RECORDS = 4_096
MAX_USAGE_WINDOWS = 64
MAX_USAGE_TEXT_BYTES = 4_096
MAX_USAGE_UTILIZATION = 100.0

_DOCUMENT_KEYS = frozenset(
    {
        "accounts",
        "identity_promotions",
        "schema_version",
    }
)
_PROMOTION_KEYS = frozenset(
    {
        "provider_id",
        "source_identity",
        "target_identity",
    }
)
_RECORD_KEYS = frozenset(
    {
        "fetched_at",
        "plan",
        "provider_id",
        "provider_identity",
        "windows",
    }
)
_WINDOW_KEYS = frozenset({"name", "resets_at", "utilization"})


class UsageSnapshotDecodeError(ValueError):
    """Persisted usage bytes violate the current strict schema."""


def _optional_bounded_text(value: str | None) -> str | None:
    return None if value is None else bounded_text(value, MAX_USAGE_TEXT_BYTES)


def _provider_text(value: str) -> str:
    ProviderId(value)
    return value


def _normalized_utilization(value: JsonValue) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError
    if not 0.0 <= value <= MAX_USAGE_UTILIZATION:
        raise ValueError
    return float(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageWindowRecord:
    """Strict normalized usage-window record."""

    name: str
    utilization: float
    resets_at: str | None

    def __post_init__(self) -> None:
        """Validate one bounded utilization window."""
        bounded_text(self.name, MAX_USAGE_TEXT_BYTES)
        object.__setattr__(
            self,
            "utilization",
            _normalized_utilization(self.utilization),
        )
        if self.resets_at is not None:
            canonical_timestamp_text(self.resets_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageSnapshotRecord:
    """Strict last-successful account usage record."""

    provider_id: str
    provider_identity: str | None
    plan: str
    windows: tuple[UsageWindowRecord, ...]
    fetched_at: str

    def __post_init__(self) -> None:
        """Validate one bounded provider usage record."""
        _provider_text(self.provider_id)
        _optional_bounded_text(self.provider_identity)
        bounded_text(self.plan, MAX_USAGE_TEXT_BYTES)
        if not isinstance(self.windows, tuple) or any(
            not isinstance(window, UsageWindowRecord)
            for window in self.windows
        ):
            raise ValueError
        if len(self.windows) > MAX_USAGE_WINDOWS:
            raise ValueError
        names = tuple(window.name for window in self.windows)
        if len(names) != len(set(names)):
            raise ValueError
        canonical_timestamp_text(self.fetched_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageIdentityPromotionRecord:
    """Secret-free intent to bind one usage record to a proven identity."""

    provider_id: str
    source_identity: str | None
    target_identity: str

    def __post_init__(self) -> None:
        """Validate one exact provider identity transition."""
        _provider_text(self.provider_id)
        _optional_bounded_text(self.source_identity)
        bounded_text(self.target_identity, MAX_USAGE_TEXT_BYTES)


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageSnapshotDocument:
    """Strict versioned account-usage document."""

    schema_version: int
    accounts: Mapping[str, UsageSnapshotRecord]
    identity_promotions: Mapping[str, UsageIdentityPromotionRecord]

    def __post_init__(self) -> None:
        """Validate bounded records and compatible identity promotions."""
        if type(self.schema_version) is not int or (
            self.schema_version != USAGE_SCHEMA_VERSION
        ):
            raise ValueError
        if not isinstance(self.accounts, Mapping) or not isinstance(
            self.identity_promotions,
            Mapping,
        ):
            raise TypeError
        accounts = dict(self.accounts)
        promotions = dict(self.identity_promotions)
        if (
            len(accounts) > MAX_USAGE_RECORDS
            or len(promotions) > MAX_USAGE_RECORDS
        ):
            raise ValueError
        for account_id, record in accounts.items():
            canonical_account_id_text(account_id)
            if not isinstance(record, UsageSnapshotRecord):
                raise TypeError
        for account_id, promotion in promotions.items():
            canonical_account_id_text(account_id)
            if not isinstance(promotion, UsageIdentityPromotionRecord):
                raise TypeError
            record = accounts.get(account_id)
            if (
                record is None
                or record.provider_id != promotion.provider_id
                or record.provider_identity
                not in {
                    promotion.source_identity,
                    promotion.target_identity,
                }
                or promotion.source_identity == promotion.target_identity
            ):
                raise ValueError
        object.__setattr__(
            self,
            "accounts",
            MappingProxyType(accounts),
        )
        object.__setattr__(
            self,
            "identity_promotions",
            MappingProxyType(promotions),
        )


def _window(value: JsonValue) -> UsageWindowRecord:
    window = require_object(value)
    require_exact_keys(window, _WINDOW_KEYS)
    return UsageWindowRecord(
        name=require_string(window["name"]),
        utilization=require_number(window["utilization"]),
        resets_at=require_optional_string(window["resets_at"]),
    )


def _record(value: JsonValue) -> UsageSnapshotRecord:
    record = require_object(value)
    require_exact_keys(record, _RECORD_KEYS)
    windows = require_list(record["windows"])
    if len(windows) > MAX_USAGE_WINDOWS:
        raise InvalidSchemaError
    return UsageSnapshotRecord(
        provider_id=require_string(record["provider_id"]),
        provider_identity=require_optional_string(record["provider_identity"]),
        plan=require_string(record["plan"]),
        windows=tuple(_window(window) for window in windows),
        fetched_at=require_string(record["fetched_at"]),
    )


def _promotion(value: JsonValue) -> UsageIdentityPromotionRecord:
    promotion = require_object(value)
    require_exact_keys(promotion, _PROMOTION_KEYS)
    return UsageIdentityPromotionRecord(
        provider_id=require_string(promotion["provider_id"]),
        source_identity=require_optional_string(promotion["source_identity"]),
        target_identity=require_string(promotion["target_identity"]),
    )


def _window_object(window: UsageWindowRecord) -> JsonObject:
    return {
        "name": window.name,
        "resets_at": window.resets_at,
        "utilization": window.utilization,
    }


def _record_object(record: UsageSnapshotRecord) -> JsonObject:
    return {
        "fetched_at": record.fetched_at,
        "plan": record.plan,
        "provider_id": record.provider_id,
        "provider_identity": record.provider_identity,
        "windows": [_window_object(window) for window in record.windows],
    }


def _promotion_object(
    promotion: UsageIdentityPromotionRecord,
) -> JsonObject:
    return {
        "provider_id": promotion.provider_id,
        "source_identity": promotion.source_identity,
        "target_identity": promotion.target_identity,
    }


def encode_usage_snapshot_document(
    document: UsageSnapshotDocument,
) -> bytes:
    """Encode one canonical account-usage document."""
    accounts: JsonObject = {
        account_id: _record_object(record)
        for account_id, record in document.accounts.items()
    }
    promotions: JsonObject = {
        account_id: _promotion_object(promotion)
        for account_id, promotion in document.identity_promotions.items()
    }
    root: JsonObject = {
        "accounts": accounts,
        "identity_promotions": promotions,
        "schema_version": document.schema_version,
    }
    return encode_compact_json(root) + b"\n"


def decode_usage_snapshot_document(
    payload: bytes,
) -> UsageSnapshotDocument:
    """Decode one canonical account-usage document."""
    try:
        root = require_object(decode_json_value(payload))
        require_exact_keys(root, _DOCUMENT_KEYS)
        require_schema_version(root["schema_version"], USAGE_SCHEMA_VERSION)
        account_values = require_object(root["accounts"])
        promotion_values = require_object(root["identity_promotions"])
        if (
            len(account_values) > MAX_USAGE_RECORDS
            or len(promotion_values) > MAX_USAGE_RECORDS
        ):
            raise InvalidSchemaError
        document = UsageSnapshotDocument(
            schema_version=USAGE_SCHEMA_VERSION,
            accounts={
                account_id: _record(value)
                for account_id, value in account_values.items()
            },
            identity_promotions={
                account_id: _promotion(value)
                for account_id, value in promotion_values.items()
            },
        )
        if encode_usage_snapshot_document(document) != payload:
            raise UsageSnapshotDecodeError
    except (
        InvalidSchemaError,
        JsonDecodeError,
        JsonEncodeError,
        TypeError,
        UsageSnapshotDecodeError,
        ValueError,
    ):
        raise UsageSnapshotDecodeError from None
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
        windows=tuple(
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
        ),
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

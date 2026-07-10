"""Private Pydantic declarations for persisted account boundaries."""

import re
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
)

_MAX_TOKEN_BYTES = 262_144
_MAX_SHORT_BYTES = 256
_MAX_METADATA_BYTES = 4_096
_MAX_AUTH_HOME_BYTES = 32_768
_MAX_SCOPES = 128
_MAX_HEARTBEAT_TARGETS = 32
_MAX_HEARTBEAT_RESETS = 32
_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def _bounded_utf8(value: str, maximum: int) -> str:
    """Require a non-empty UTF-8 string within ``maximum`` bytes."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError from error
    if not encoded or len(encoded) > maximum:
        raise ValueError
    return value


def _short_string(value: str) -> str:
    return _bounded_utf8(value, _MAX_SHORT_BYTES)


def _metadata_string(value: str) -> str:
    return _bounded_utf8(value, _MAX_METADATA_BYTES)


def _token_string(value: str) -> str:
    return _bounded_utf8(value, _MAX_TOKEN_BYTES)


def _auth_home_string(value: str) -> str:
    return _bounded_utf8(value, _MAX_AUTH_HOME_BYTES)


def _timestamp_string(value: str) -> str:
    return _bounded_utf8(value, 32)


def _sha256_string(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise ValueError
    return value


def _scopes(value: list[str]) -> list[str]:
    if len(value) > _MAX_SCOPES or len(value) != len(set(value)):
        raise ValueError
    return value


def _heartbeat_targets(value: list[str]) -> list[str]:
    if len(value) > _MAX_HEARTBEAT_TARGETS or len(value) != len(set(value)):
        raise ValueError
    return value


def _heartbeat_resets(value: dict[str, str]) -> dict[str, str]:
    if len(value) > _MAX_HEARTBEAT_RESETS:
        raise ValueError
    return value


type _ShortString = Annotated[str, AfterValidator(_short_string)]
type _MetadataString = Annotated[str, AfterValidator(_metadata_string)]
type _TokenString = Annotated[str, AfterValidator(_token_string)]
type _AuthHomeString = Annotated[str, AfterValidator(_auth_home_string)]
type _TimestampString = Annotated[str, AfterValidator(_timestamp_string)]
type _Sha256String = Annotated[str, AfterValidator(_sha256_string)]
type _ScopeList = Annotated[
    list[_MetadataString],
    AfterValidator(_scopes),
]
type _HeartbeatTargetList = Annotated[
    list[_ShortString],
    AfterValidator(_heartbeat_targets),
]
type _HeartbeatResetMap = Annotated[
    dict[_ShortString, _TimestampString],
    AfterValidator(_heartbeat_resets),
]
type _RefreshStatusValue = Literal["ok", "skipped", "failed"]
type _HeartbeatStatusValue = Literal[
    "warmed",
    "active",
    "disabled",
    "unsupported",
    "failed",
    "enabled",
]


class _AccountRecordModel(BaseModel):
    """Strict fields shared by both provider variants."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    provider_id: str
    provider_account_id: _MetadataString | None
    access_token: _TokenString = Field(repr=False)
    refresh_token: _TokenString | None = Field(repr=False)
    expires_at: int | _TimestampString | None
    plan: _ShortString
    scopes: _ScopeList | None
    codex_home: _AuthHomeString | None
    codex_id_token: _TokenString | None = Field(repr=False)
    codex_last_refresh: _MetadataString | None
    last_refresh_at: _TimestampString | None
    last_refresh_status: _RefreshStatusValue | None
    last_refresh_error: _MetadataString | None
    heartbeat_enabled: bool
    heartbeat_5h_reset_at: _TimestampString | None
    heartbeat_window_resets: _HeartbeatResetMap | None
    heartbeat_targets: _HeartbeatTargetList | None
    last_heartbeat_at: _TimestampString | None
    last_heartbeat_status: _HeartbeatStatusValue | None
    last_heartbeat_error: _MetadataString | None


class _ClaudeRecordModel(_AccountRecordModel):
    """Strict flattened Claude persistence record."""

    provider_id: Literal["claude"]
    provider_account_id: None
    codex_home: None
    codex_id_token: None = Field(repr=False)
    codex_last_refresh: None


class _CodexRecordModel(_AccountRecordModel):
    """Strict flattened Codex persistence record."""

    provider_id: Literal["codex"]
    scopes: None


type ValidatedAccountRecord = Annotated[
    _ClaudeRecordModel | _CodexRecordModel,
    Field(discriminator="provider_id"),
]


class VersionOneEnvelopeModel(BaseModel):
    """Strict schema-version-one envelope."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[1]
    accounts: dict[str, ValidatedAccountRecord]


class PrototypeRecordModel(BaseModel):
    """Strict import-only prototype record."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    token: _TokenString = Field(repr=False)
    plan: _ShortString


class PrototypeReceiptModel(BaseModel):
    """Strict prototype-import receipt."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    receipt_version: Literal[1]
    prototype_sha256: _Sha256String
    target_schema_version: Literal[1]


GENERATION_ZERO_ADAPTER = TypeAdapter(dict[str, ValidatedAccountRecord])
VERSION_ONE_ADAPTER = TypeAdapter(VersionOneEnvelopeModel)
PROTOTYPE_ADAPTER = TypeAdapter(dict[str, PrototypeRecordModel])
PROTOTYPE_RECEIPT_ADAPTER = TypeAdapter(PrototypeReceiptModel)

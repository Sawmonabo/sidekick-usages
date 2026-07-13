"""Strict schema-version-two account codec implementation."""

from dataclasses import dataclass
from datetime import datetime

from sidekick_usages.core.types import (
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.persistence import schemas as _schemas
from sidekick_usages.persistence._schema_models import (
    VERSION_TWO_ADAPTER,
    CurrentClaudeLoginRecordModel,
    CurrentClaudeSetupRecordModel,
)
from sidekick_usages.persistence.credential_ownership import (
    reject_duplicate_credential_ownership,
)
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.serialization import JsonObject

_CURRENT_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class _CurrentStateValues:
    """Typed provider-neutral state decoded from a current record."""

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


def _state_values(
    model: CurrentClaudeSetupRecordModel | CurrentClaudeLoginRecordModel,
) -> _CurrentStateValues:
    last_refresh_at = model.last_refresh_at
    last_refresh_status = model.last_refresh_status
    heartbeat_5h_reset_at = model.heartbeat_5h_reset_at
    heartbeat_window_resets = model.heartbeat_window_resets
    last_heartbeat_at = model.last_heartbeat_at
    last_heartbeat_status = model.last_heartbeat_status
    return _CurrentStateValues(
        last_refresh_at=_schemas._stored_timestamp(
            last_refresh_at,
            canonical=True,
        ),
        last_refresh_status=(
            RefreshStatus(last_refresh_status)
            if last_refresh_status is not None
            else None
        ),
        last_refresh_error=model.last_refresh_error,
        heartbeat_enabled=model.heartbeat_enabled,
        heartbeat_5h_reset_at=_schemas._stored_timestamp(
            heartbeat_5h_reset_at,
            canonical=True,
        ),
        heartbeat_window_resets=(
            tuple(
                (target_id, _schemas._parse_canonical_timestamp(reset_at))
                for target_id, reset_at in heartbeat_window_resets.items()
            )
            if heartbeat_window_resets is not None
            else None
        ),
        heartbeat_targets=(
            tuple(model.heartbeat_targets)
            if model.heartbeat_targets is not None
            else None
        ),
        last_heartbeat_at=_schemas._stored_timestamp(
            last_heartbeat_at,
            canonical=True,
        ),
        last_heartbeat_status=(
            HeartbeatStatus(last_heartbeat_status)
            if last_heartbeat_status is not None
            else None
        ),
        last_heartbeat_error=model.last_heartbeat_error,
    )


def _claude_record(
    label: str,
    model: CurrentClaudeSetupRecordModel | CurrentClaudeLoginRecordModel,
) -> _schemas.StoredAccountRecord:
    state = _state_values(model)
    if isinstance(model, CurrentClaudeSetupRecordModel):
        return _schemas.StoredAccountRecord(
            label=_schemas._validated_label(label),
            provider_id=ProviderId.CLAUDE,
            provider_account_id=None,
            access_token=model.access_token,
            refresh_token=None,
            expires_at=None,
            plan=model.plan,
            scopes=None,
            codex_home=None,
            codex_id_token=None,
            codex_last_refresh=None,
            last_refresh_at=state.last_refresh_at,
            last_refresh_status=state.last_refresh_status,
            last_refresh_error=state.last_refresh_error,
            heartbeat_enabled=state.heartbeat_enabled,
            heartbeat_5h_reset_at=state.heartbeat_5h_reset_at,
            heartbeat_window_resets=state.heartbeat_window_resets,
            heartbeat_targets=state.heartbeat_targets,
            last_heartbeat_at=state.last_heartbeat_at,
            last_heartbeat_status=state.last_heartbeat_status,
            last_heartbeat_error=state.last_heartbeat_error,
            credential_kind=_schemas.ClaudeCredentialKind.SETUP_TOKEN,
        )
    access_expiry = _schemas._parse_canonical_timestamp(
        model.access_expires_at
    )
    _schemas._expiry_native_value(ProviderId.CLAUDE, access_expiry)
    refresh_expiry = (
        _schemas._parse_canonical_timestamp(model.refresh_expires_at)
        if model.refresh_expires_at is not None
        else None
    )
    if refresh_expiry is not None:
        _schemas._expiry_native_value(ProviderId.CLAUDE, refresh_expiry)
    identity = (
        _schemas.StoredClaudeIdentity(
            model.claude_identity.account_id,
            model.claude_identity.organization_id,
        )
        if model.claude_identity is not None
        else None
    )
    return _schemas.StoredAccountRecord(
        label=_schemas._validated_label(label),
        provider_id=ProviderId.CLAUDE,
        provider_account_id=None,
        access_token=model.access_token,
        refresh_token=model.refresh_token,
        expires_at=access_expiry,
        plan=model.plan,
        scopes=tuple(model.scopes),
        codex_home=None,
        codex_id_token=None,
        codex_last_refresh=None,
        last_refresh_at=state.last_refresh_at,
        last_refresh_status=state.last_refresh_status,
        last_refresh_error=state.last_refresh_error,
        heartbeat_enabled=state.heartbeat_enabled,
        heartbeat_5h_reset_at=state.heartbeat_5h_reset_at,
        heartbeat_window_resets=state.heartbeat_window_resets,
        heartbeat_targets=state.heartbeat_targets,
        last_heartbeat_at=state.last_heartbeat_at,
        last_heartbeat_status=state.last_heartbeat_status,
        last_heartbeat_error=state.last_heartbeat_error,
        credential_kind=_schemas.ClaudeCredentialKind.SUBSCRIPTION_LOGIN,
        refresh_expires_at=refresh_expiry,
        claude_identity=identity,
    )


def decode_current(payload: bytes) -> _schemas.VersionTwoDocument:
    """Decode strict schema-version-two bytes."""
    root = _schemas._object_root(payload)
    schema_version = root.get("schema_version")
    if (
        schema_version != _CURRENT_SCHEMA_VERSION
        or type(schema_version) is not int
    ):
        raise InvalidSchemaError
    accounts_value = root.get("accounts")
    if isinstance(accounts_value, dict):
        _schemas._validate_account_keys(accounts_value)
    envelope = _schemas._validate(VERSION_TWO_ADAPTER, root)
    records: list[_schemas.StoredAccountRecord] = []
    for label, model in envelope.accounts.items():
        if isinstance(
            model,
            CurrentClaudeSetupRecordModel | CurrentClaudeLoginRecordModel,
        ):
            records.append(_claude_record(label, model))
        else:
            records.append(
                _schemas._record_from_model(label, model, version_one=True)
            )

    result = _schemas.VersionTwoDocument(tuple(records))
    reject_duplicate_credential_ownership(result.accounts)
    return result


_STATE_FIELDS = (
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


def _current_record_object(
    record: _schemas.StoredAccountRecord,
) -> JsonObject:
    legacy = _schemas._record_object(record, version_one=True)
    state = {field: legacy[field] for field in _STATE_FIELDS}
    if record.provider_id is ProviderId.CODEX:
        if (
            record.credential_kind is not None
            or record.refresh_expires_at is not None
            or record.claude_identity is not None
        ):
            raise InvalidSchemaError
        return legacy
    if record.credential_kind is _schemas.ClaudeCredentialKind.SETUP_TOKEN:
        if any(
            value is not None
            for value in (
                record.refresh_token,
                record.expires_at,
                record.scopes,
                record.refresh_expires_at,
                record.claude_identity,
            )
        ):
            raise InvalidSchemaError
        return {
            "provider_id": "claude",
            "credential_kind": "setup_token",
            "access_token": record.access_token,
            "plan": record.plan,
            **state,
        }
    if (
        record.credential_kind
        is not _schemas.ClaudeCredentialKind.SUBSCRIPTION_LOGIN
        or record.refresh_token is None
        or record.expires_at is None
        or record.scopes is None
    ):
        raise InvalidSchemaError
    _schemas._expiry_native_value(ProviderId.CLAUDE, record.expires_at)
    refresh_expiry = (
        _schemas._canonical_timestamp(record.refresh_expires_at)
        if record.refresh_expires_at is not None
        else None
    )
    identity: JsonObject | None = None
    if record.claude_identity is not None:
        identity = {
            "account_id": record.claude_identity.account_id,
            "organization_id": record.claude_identity.organization_id,
        }
    return {
        "provider_id": "claude",
        "credential_kind": "subscription_login",
        "access_token": record.access_token,
        "refresh_token": record.refresh_token,
        "access_expires_at": _schemas._canonical_timestamp(record.expires_at),
        "refresh_expires_at": refresh_expiry,
        "scopes": list(record.scopes),
        "claude_identity": identity,
        "plan": record.plan,
        **state,
    }


def encode_current(document: _schemas.VersionTwoDocument) -> bytes:
    """Encode and strictly re-decode current account state."""
    accounts: JsonObject = {}
    for record in document.accounts:
        label = _schemas._validated_label(str(record.label))
        if label in accounts:
            raise InvalidSchemaError
        accounts[label] = _current_record_object(record)
    payload = _schemas._encode_json(
        {"schema_version": _CURRENT_SCHEMA_VERSION, "accounts": accounts}
    )
    decode_current(payload)
    return payload

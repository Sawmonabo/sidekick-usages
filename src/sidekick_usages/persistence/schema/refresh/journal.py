"""Strict non-secret credential-refresh journal codec."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ValidationError,
)

from sidekick_usages.core.models import (
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
    Credentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.persistence.schema.config import STRICT_SCHEMA_CONFIG
from sidekick_usages.persistence.schema.credential import (
    encode_credentials,
)
from sidekick_usages.persistence.schema.validation import sha256_text
from sidekick_usages.persistence.time_codec import canonical_timestamp_text
from sidekick_usages.serialization.json import (
    JsonDecodeError,
    decode_json_value,
)

ACCOUNT_KEY_DOMAIN = b"sidekick-usages credential refresh account\0"
CREDENTIAL_DOMAIN = b"sidekick-usages credential refresh credential\0"
JOURNAL_BASENAME = "intent.json"
STAGE_BASENAME = "replacement.json"
JOURNAL_SCHEMA_VERSION = 1

type _Sha256Value = Annotated[str, AfterValidator(sha256_text)]
type _TimestampValue = Annotated[
    str,
    AfterValidator(canonical_timestamp_text),
]
type CredentialKind = Literal["subscription_login", "codex_login"]
type RefreshReason = Literal[
    "scheduled_due",
    "access_rejected",
    "credential_required",
    "operator_forced",
]
type StageState = Literal[
    "intent",
    "complete",
    "committed",
    "durability_uncertain",
]


class RefreshJournalDecodeError(ValueError):
    """Private refresh journal or stage violates its strict contract."""


class RefreshJournal(BaseModel):
    """Strict non-secret refresh intent and stage proof."""

    model_config = STRICT_SCHEMA_CONFIG

    schema_version: Literal[1]
    provider_id: Literal["claude", "codex"]
    account_key_digest: _Sha256Value
    expected_credential_kind: CredentialKind
    expected_credential_sha256: _Sha256Value
    operation_started_at: _TimestampValue
    refresh_reason: RefreshReason
    stage_state: StageState
    staged_credential_sha256: _Sha256Value | None


def account_key_digest(
    provider_id: ProviderId,
    label: AccountLabel,
) -> str:
    """Return provider-qualified non-secret journal routing metadata."""
    return hashlib.sha256(
        ACCOUNT_KEY_DOMAIN
        + provider_id.value.encode("utf-8")
        + b"\0"
        + str(label).encode("utf-8")
    ).hexdigest()


def credential_digest(credentials: Credentials) -> str:
    """Digest one canonical secret-bearing credential record."""
    payload = encode_credentials(credentials)
    return hashlib.sha256(CREDENTIAL_DOMAIN + payload).hexdigest()


def refresh_credential_kind(credentials: Credentials) -> CredentialKind:
    """Return the exact persisted rotating credential kind."""
    if isinstance(credentials, ClaudeLoginCredentials):
        return "subscription_login"
    if isinstance(credentials, CodexCredentials):
        return "codex_login"
    if isinstance(credentials, ClaudeSetupTokenCredentials):
        raise ValueError("Setup-token credentials do not rotate.")
    raise TypeError("Unsupported credential variant.")


def refresh_reason(value: str) -> RefreshReason:
    """Validate and narrow one application refresh reason."""
    if value == "scheduled_due":
        return "scheduled_due"
    if value == "access_rejected":
        return "access_rejected"
    if value == "credential_required":
        return "credential_required"
    if value == "operator_forced":
        return "operator_forced"
    raise ValueError("Credential refresh reason is invalid.")


def encode_refresh_journal(journal: RefreshJournal) -> bytes:
    """Encode and strictly re-decode a non-secret refresh journal."""
    payload = json.dumps(
        journal.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    decode_refresh_journal(payload)
    return payload


def decode_refresh_journal(payload: bytes) -> RefreshJournal:
    """Decode one strict non-secret refresh journal."""
    try:
        value = decode_json_value(payload)
        return RefreshJournal.model_validate(value, strict=True)
    except JsonDecodeError, ValidationError:
        raise RefreshJournalDecodeError from None

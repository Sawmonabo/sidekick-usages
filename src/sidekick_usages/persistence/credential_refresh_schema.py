"""Strict non-secret journal and secret stage codec for refresh recovery."""

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    TypeAdapter,
    ValidationError,
)

from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
    Credentials,
)
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.schemas import (
    VersionTwoDocument,
    decode_version_two,
    encode_version_two,
)
from sidekick_usages.persistence.transforms import (
    accounts_to_version_two,
    version_two_to_accounts,
)
from sidekick_usages.serialization import JsonDecodeError, decode_json_value

LABEL_DOMAIN = b"sidekick-usages credential refresh label\0"
CREDENTIAL_DOMAIN = b"sidekick-usages credential refresh credential\0"
JOURNAL_BASENAME = "intent.json"
STAGE_BASENAME = "replacement.json"
JOURNAL_SCHEMA_VERSION = 1
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_TIMESTAMP_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z",
    re.ASCII,
)


class RefreshJournalDecodeError(ValueError):
    """Private refresh journal or stage violates its strict contract."""


def require_sha256(value: str) -> str:
    """Require one lower-case SHA-256 hexadecimal value."""
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError
    return value


def _timestamp_value(value: str) -> str:
    if _TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError
    return value


type _Sha256Value = Annotated[str, AfterValidator(require_sha256)]
type _TimestampValue = Annotated[str, AfterValidator(_timestamp_value)]
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


class RefreshJournal(BaseModel):
    """Strict non-secret refresh intent and stage proof."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[1]
    provider_id: Literal["claude", "codex"]
    account_label_digest: _Sha256Value
    expected_credential_kind: CredentialKind
    expected_credential_sha256: _Sha256Value
    operation_started_at: _TimestampValue
    refresh_reason: RefreshReason
    stage_state: StageState
    staged_credential_sha256: _Sha256Value | None


_JOURNAL_ADAPTER = TypeAdapter(RefreshJournal)


def label_digest(label: AccountLabel) -> str:
    """Return domain-separated non-secret journal routing metadata."""
    return hashlib.sha256(
        LABEL_DOMAIN + str(label).encode("utf-8")
    ).hexdigest()


def credential_digest(credentials: Credentials) -> str:
    """Digest one canonical secret-bearing credential record."""
    record = Account(
        label=AccountLabel("credential-record"),
        credentials=credentials,
    )
    payload = encode_version_two(accounts_to_version_two((record,)))
    return hashlib.sha256(CREDENTIAL_DOMAIN + payload).hexdigest()


def credential_kind(credentials: Credentials) -> CredentialKind:
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


def refresh_timestamp(value: datetime) -> str:
    """Encode one aware instant in the canonical journal form."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Credential refresh time must be aware.")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
        return _JOURNAL_ADAPTER.validate_python(value, strict=True)
    except JsonDecodeError, ValidationError:
        raise RefreshJournalDecodeError from None


def decode_staged_account(payload: bytes) -> Account:
    """Decode exactly one strict schema-v2 staged target account."""
    try:
        document: VersionTwoDocument = decode_version_two(payload)
        accounts = version_two_to_accounts(document)
    except PersistenceError, TypeError, ValueError:
        raise RefreshJournalDecodeError from None
    if len(accounts) != 1:
        raise RefreshJournalDecodeError
    return accounts[0]


__all__ = [
    "JOURNAL_BASENAME",
    "JOURNAL_SCHEMA_VERSION",
    "STAGE_BASENAME",
    "RefreshJournal",
    "RefreshJournalDecodeError",
    "credential_digest",
    "credential_kind",
    "decode_refresh_journal",
    "decode_staged_account",
    "encode_refresh_journal",
    "label_digest",
    "refresh_reason",
    "refresh_timestamp",
    "require_sha256",
]

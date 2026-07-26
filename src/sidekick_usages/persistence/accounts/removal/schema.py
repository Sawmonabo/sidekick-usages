"""Strict codec for durable account-removal records."""

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.accounts.removal.models import (
    AccountRemovalDocument,
    AccountRemovalPhase,
    AccountRemovalRecord,
)
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.state.fields import (
    require_exact_keys,
    require_object,
    require_optional_string,
    require_schema_version,
    require_string,
)
from sidekick_usages.persistence.state.json import (
    decode_state_object,
    encode_state_object,
)
from sidekick_usages.persistence.types.artifact import Sha256Digest
from sidekick_usages.serialization.json import JsonObject

ACCOUNT_REMOVAL_SCHEMA_VERSION = 1
MAX_ACCOUNT_REMOVAL_BYTES = 256 * 1024

_RECORD_KEYS = frozenset(
    {
        "expected_account_digest",
        "phase",
        "provider_id",
    }
)


def decode_account_removals(payload: bytes) -> AccountRemovalDocument:
    """Decode one canonical bounded account-removal document."""
    root = decode_state_object(payload, MAX_ACCOUNT_REMOVAL_BYTES)
    require_exact_keys(root, {"records", "schema_version"})
    require_schema_version(
        root["schema_version"],
        ACCOUNT_REMOVAL_SCHEMA_VERSION,
    )
    records = require_object(root["records"])
    try:
        document = AccountRemovalDocument(
            tuple(
                _record(account_id, require_object(value))
                for account_id, value in records.items()
            )
        )
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    if _payload(document) != payload:
        raise InvalidSchemaError
    return document


def encode_account_removals(document: AccountRemovalDocument) -> bytes:
    """Encode and prove one canonical account-removal document."""
    payload = _payload(document)
    if decode_account_removals(payload) != document:
        raise InvalidSchemaError
    return payload


def _record(
    account_id: str,
    value: JsonObject,
) -> AccountRemovalRecord:
    require_exact_keys(value, _RECORD_KEYS)
    digest = require_optional_string(value["expected_account_digest"])
    return AccountRemovalRecord(
        account_id=SidekickAccountId(account_id),
        provider_id=ProviderId(require_string(value["provider_id"])),
        expected_account_digest=(
            None if digest is None else Sha256Digest(digest)
        ),
        phase=AccountRemovalPhase(require_string(value["phase"])),
    )


def _payload(document: AccountRemovalDocument) -> bytes:
    root: JsonObject = {
        "records": {
            str(record.account_id): {
                "expected_account_digest": (
                    None
                    if record.expected_account_digest is None
                    else str(record.expected_account_digest)
                ),
                "phase": record.phase.value,
                "provider_id": record.provider_id.value,
            }
            for record in document.records
        },
        "schema_version": ACCOUNT_REMOVAL_SCHEMA_VERSION,
    }
    return encode_state_object(root, MAX_ACCOUNT_REMOVAL_BYTES)

"""Strict lightweight codecs for selection and durable operation state."""

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    DueOperation,
    SelectedAccountState,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    OperationKind,
    OperationPriority,
    OperationState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.models.selection import (
    MAX_ACTIVATION_HISTORY,
    MAX_OPERATION_RECORDS,
    ActivationJournalDocument,
    OperationQueueDocument,
    SelectedStateDocument,
)
from sidekick_usages.persistence.state.fields import (
    require_exact_keys,
    require_integer,
    require_list,
    require_object,
    require_optional_string,
    require_schema_version,
    require_string,
)
from sidekick_usages.persistence.state.json import (
    decode_state_object,
    encode_state_object,
)
from sidekick_usages.persistence.time_codec import (
    canonical_timestamp,
    parse_canonical_timestamp,
)
from sidekick_usages.serialization.json import JsonObject, JsonValue

STATE_SCHEMA_VERSION = 1
MAX_SELECTED_STATE_BYTES = 256 * 1024
MAX_ACTIVATION_JOURNAL_BYTES = 512 * 1024
MAX_OPERATION_QUEUE_BYTES = 8 * 1024 * 1024

_SELECTED_KEYS = frozenset(
    {
        "account_id",
        "outcome",
        "provider_identity",
        "runtime_generation",
        "runtime_state",
        "verified_at",
    }
)
_ACTIVATION_KEYS = frozenset(
    {
        "expected_target_identity",
        "failure_code",
        "operation_id",
        "outcome",
        "phase",
        "provider_id",
        "source_account_id",
        "source_generation",
        "source_provider_identity",
        "started_at",
        "target_account_id",
        "updated_at",
    }
)
_OPERATION_KEYS = frozenset(
    {
        "account_id",
        "attempts",
        "due_at",
        "failure_code",
        "kind",
        "operation_id",
        "priority",
        "provider_id",
        "state",
        "updated_at",
    }
)


def decode_selected_state(payload: bytes) -> SelectedStateDocument:
    """Decode one canonical provider selected-state document."""
    root = decode_state_object(payload, MAX_SELECTED_STATE_BYTES)
    require_exact_keys(root, {"providers", "schema_version"})
    require_schema_version(root["schema_version"], STATE_SCHEMA_VERSION)
    providers = require_object(root["providers"])
    if len(providers) > len(ProviderId):
        raise InvalidSchemaError
    try:
        states = tuple(
            _selected_state(ProviderId(name), require_object(value))
            for name, value in providers.items()
        )
        document = SelectedStateDocument(states)
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    if _selected_payload(document) != payload:
        raise InvalidSchemaError
    return document


def encode_selected_state(document: SelectedStateDocument) -> bytes:
    """Encode one canonical provider selected-state document."""
    payload = _selected_payload(document)
    if decode_selected_state(payload) != document:
        raise InvalidSchemaError
    return payload


def decode_activation_journal(
    payload: bytes,
) -> ActivationJournalDocument:
    """Decode one canonical provider activation journal."""
    root = decode_state_object(payload, MAX_ACTIVATION_JOURNAL_BYTES)
    require_exact_keys(
        root,
        {"active", "history", "provider_id", "schema_version"},
    )
    require_schema_version(root["schema_version"], STATE_SCHEMA_VERSION)
    try:
        provider_id = ProviderId(require_string(root["provider_id"]))
        active_value = root["active"]
        active = (
            None
            if active_value is None
            else _activation_record(require_object(active_value))
        )
        history_values = require_list(root["history"])
        if len(history_values) > MAX_ACTIVATION_HISTORY:
            raise InvalidSchemaError
        document = ActivationJournalDocument(
            provider_id=provider_id,
            active=active,
            history=tuple(
                _activation_record(require_object(value))
                for value in history_values
            ),
        )
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    if _activation_payload(document) != payload:
        raise InvalidSchemaError
    return document


def encode_activation_journal(
    document: ActivationJournalDocument,
) -> bytes:
    """Encode one canonical provider activation journal."""
    payload = _activation_payload(document)
    if decode_activation_journal(payload) != document:
        raise InvalidSchemaError
    return payload


def operation_slot(
    account_id: SidekickAccountId,
    kind: OperationKind,
) -> str:
    """Return one canonical durable account-operation slot key."""
    return f"{account_id}:{kind.value}"


def decode_operation_queue(payload: bytes) -> OperationQueueDocument:
    """Decode one canonical durable operation queue."""
    root = decode_state_object(payload, MAX_OPERATION_QUEUE_BYTES)
    require_exact_keys(root, {"operations", "schema_version"})
    require_schema_version(root["schema_version"], STATE_SCHEMA_VERSION)
    records = require_object(root["operations"])
    if len(records) > MAX_OPERATION_RECORDS:
        raise InvalidSchemaError
    try:
        operations: list[DueOperation] = []
        for slot, value in records.items():
            operation = _due_operation(require_object(value))
            if slot != operation_slot(
                operation.account_id,
                operation.kind,
            ):
                raise InvalidSchemaError
            operations.append(operation)
        document = OperationQueueDocument(tuple(operations))
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    if _operation_payload(document) != payload:
        raise InvalidSchemaError
    return document


def encode_operation_queue(document: OperationQueueDocument) -> bytes:
    """Encode one canonical durable operation queue."""
    payload = _operation_payload(document)
    if decode_operation_queue(payload) != document:
        raise InvalidSchemaError
    return payload


def _selected_state(
    provider_id: ProviderId,
    record: JsonObject,
) -> SelectedAccountState:
    require_exact_keys(record, _SELECTED_KEYS)
    account_id = require_optional_string(record["account_id"])
    identity = require_optional_string(record["provider_identity"])
    generation = require_optional_string(record["runtime_generation"])
    return SelectedAccountState(
        provider_id=provider_id,
        runtime_state=ProviderRuntimeState(
            require_string(record["runtime_state"])
        ),
        account_id=(
            None if account_id is None else SidekickAccountId(account_id)
        ),
        provider_identity=(
            None if identity is None else ProviderIdentity(identity)
        ),
        runtime_generation=(
            None if generation is None else AuthorityGeneration(generation)
        ),
        verified_at=parse_canonical_timestamp(
            require_string(record["verified_at"])
        ),
        outcome=ActivationOutcome(require_string(record["outcome"])),
    )


def _selected_object(state: SelectedAccountState) -> JsonObject:
    return {
        "account_id": (
            None if state.account_id is None else str(state.account_id)
        ),
        "outcome": state.outcome.value,
        "provider_identity": (
            None
            if state.provider_identity is None
            else str(state.provider_identity)
        ),
        "runtime_generation": (
            None
            if state.runtime_generation is None
            else str(state.runtime_generation)
        ),
        "runtime_state": state.runtime_state.value,
        "verified_at": canonical_timestamp(state.verified_at),
    }


def _selected_payload(document: SelectedStateDocument) -> bytes:
    providers: JsonObject = {
        state.provider_id.value: _selected_object(state)
        for state in document.states
    }
    return encode_state_object(
        {
            "providers": providers,
            "schema_version": STATE_SCHEMA_VERSION,
        },
        MAX_SELECTED_STATE_BYTES,
    )


def _activation_record(record: JsonObject) -> ActivationRecord:
    require_exact_keys(record, _ACTIVATION_KEYS)
    source_account = require_optional_string(record["source_account_id"])
    source_identity = require_optional_string(
        record["source_provider_identity"]
    )
    source_generation = require_optional_string(record["source_generation"])
    outcome = require_optional_string(record["outcome"])
    return ActivationRecord(
        provider_id=ProviderId(require_string(record["provider_id"])),
        operation_id=OperationId(require_string(record["operation_id"])),
        source_account_id=(
            None
            if source_account is None
            else SidekickAccountId(source_account)
        ),
        target_account_id=SidekickAccountId(
            require_string(record["target_account_id"])
        ),
        source_provider_identity=(
            None
            if source_identity is None
            else ProviderIdentity(source_identity)
        ),
        source_generation=(
            None
            if source_generation is None
            else AuthorityGeneration(source_generation)
        ),
        expected_target_identity=ProviderIdentity(
            require_string(record["expected_target_identity"])
        ),
        phase=ActivationPhase(require_string(record["phase"])),
        started_at=parse_canonical_timestamp(
            require_string(record["started_at"])
        ),
        updated_at=parse_canonical_timestamp(
            require_string(record["updated_at"])
        ),
        outcome=(None if outcome is None else ActivationOutcome(outcome)),
        failure_code=require_optional_string(record["failure_code"]),
    )


def _activation_object(record: ActivationRecord) -> JsonObject:
    return {
        "expected_target_identity": str(record.expected_target_identity),
        "failure_code": record.failure_code,
        "operation_id": str(record.operation_id),
        "outcome": (None if record.outcome is None else record.outcome.value),
        "phase": record.phase.value,
        "provider_id": record.provider_id.value,
        "source_account_id": (
            None
            if record.source_account_id is None
            else str(record.source_account_id)
        ),
        "source_generation": (
            None
            if record.source_generation is None
            else str(record.source_generation)
        ),
        "source_provider_identity": (
            None
            if record.source_provider_identity is None
            else str(record.source_provider_identity)
        ),
        "started_at": canonical_timestamp(record.started_at),
        "target_account_id": str(record.target_account_id),
        "updated_at": canonical_timestamp(record.updated_at),
    }


def _activation_payload(document: ActivationJournalDocument) -> bytes:
    history: list[JsonValue] = [
        _activation_object(record) for record in document.history
    ]
    return encode_state_object(
        {
            "active": (
                None
                if document.active is None
                else _activation_object(document.active)
            ),
            "history": history,
            "provider_id": document.provider_id.value,
            "schema_version": STATE_SCHEMA_VERSION,
        },
        MAX_ACTIVATION_JOURNAL_BYTES,
    )


def _due_operation(record: JsonObject) -> DueOperation:
    require_exact_keys(record, _OPERATION_KEYS)
    return DueOperation(
        operation_id=OperationId(require_string(record["operation_id"])),
        provider_id=ProviderId(require_string(record["provider_id"])),
        account_id=SidekickAccountId(require_string(record["account_id"])),
        kind=OperationKind(require_string(record["kind"])),
        priority=OperationPriority(require_string(record["priority"])),
        state=OperationState(require_string(record["state"])),
        due_at=parse_canonical_timestamp(require_string(record["due_at"])),
        updated_at=parse_canonical_timestamp(
            require_string(record["updated_at"])
        ),
        attempts=require_integer(record["attempts"]),
        failure_code=require_optional_string(record["failure_code"]),
    )


def _operation_object(operation: DueOperation) -> JsonObject:
    return {
        "account_id": str(operation.account_id),
        "attempts": operation.attempts,
        "due_at": canonical_timestamp(operation.due_at),
        "failure_code": operation.failure_code,
        "kind": operation.kind.value,
        "operation_id": str(operation.operation_id),
        "priority": operation.priority.value,
        "provider_id": operation.provider_id.value,
        "state": operation.state.value,
        "updated_at": canonical_timestamp(operation.updated_at),
    }


def _operation_payload(document: OperationQueueDocument) -> bytes:
    operations: JsonObject = {
        operation_slot(operation.account_id, operation.kind): (
            _operation_object(operation)
        )
        for operation in document.operations
    }
    return encode_state_object(
        {
            "operations": operations,
            "schema_version": STATE_SCHEMA_VERSION,
        },
        MAX_OPERATION_QUEUE_BYTES,
    )

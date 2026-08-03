"""Strict non-secret codecs for durable worker inputs and results."""

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    RelatedRuntimeAuthority,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.worker import (
    SelectionWorkerMetadata,
    WorkerResult,
)
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.models.selection import (
    MAX_OPERATION_RECORDS,
    OperationQueueDocument,
)
from sidekick_usages.persistence.state.fields import (
    require_boolean,
    require_exact_keys,
    require_integer,
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

OPERATION_QUEUE_SCHEMA_VERSION = 5
_PREVIOUS_OPERATION_QUEUE_SCHEMA_VERSION = 4
_LEGACY_OPERATION_QUEUE_SCHEMA_VERSION = 3
WORKER_RESULT_SCHEMA_VERSION = 3
_LEGACY_WORKER_RESULT_SCHEMA_VERSION = 2
MAX_WORKER_RESULT_BYTES = 16 * 1024
MAX_OPERATION_QUEUE_BYTES = 8 * 1024 * 1024

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
        "selection_operation_id",
        "state",
        "updated_at",
    }
)
_PREVIOUS_OPERATION_KEYS = _OPERATION_KEYS - {"selection_operation_id"}
_LEGACY_OPERATION_KEYS = _PREVIOUS_OPERATION_KEYS | {
    "allow_remote_control_disconnect"
}

_WORKER_RESULT_KEYS = frozenset(
    {
        "failure_code",
        "finished_at",
        "operation_id",
        "outcome",
        "related_runtime_authority",
        "schema_version",
        "selection",
    }
)
_LEGACY_WORKER_RESULT_KEYS = _WORKER_RESULT_KEYS - {"selection"}


def decode_operation_queue(payload: bytes) -> OperationQueueDocument:
    """Decode one canonical durable operation queue."""
    root = decode_state_object(payload, MAX_OPERATION_QUEUE_BYTES)
    require_exact_keys(root, {"operations", "schema_version"})
    version = require_integer(root["schema_version"])
    legacy = version == _LEGACY_OPERATION_QUEUE_SCHEMA_VERSION
    previous = version == _PREVIOUS_OPERATION_QUEUE_SCHEMA_VERSION
    require_schema_version(
        version,
        (
            _LEGACY_OPERATION_QUEUE_SCHEMA_VERSION
            if legacy
            else (
                _PREVIOUS_OPERATION_QUEUE_SCHEMA_VERSION
                if previous
                else OPERATION_QUEUE_SCHEMA_VERSION
            )
        ),
    )
    records = require_object(root["operations"])
    if len(records) > MAX_OPERATION_RECORDS:
        raise InvalidSchemaError
    try:
        operations: list[DueOperation] = []
        for slot, value in records.items():
            operation = _due_operation(
                require_object(value),
                legacy=legacy,
                previous=previous,
            )
            if slot != _operation_slot(operation):
                raise InvalidSchemaError
            operations.append(operation)
        document = OperationQueueDocument(tuple(operations))
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    expected = (
        encode_state_object(root, MAX_OPERATION_QUEUE_BYTES)
        if legacy or previous
        else _operation_payload(document)
    )
    if expected != payload:
        raise InvalidSchemaError
    return document


def encode_operation_queue(document: OperationQueueDocument) -> bytes:
    """Encode one canonical durable operation queue."""
    payload = _operation_payload(document)
    if decode_operation_queue(payload) != document:
        raise InvalidSchemaError
    return payload


def _due_operation(
    record: JsonObject,
    *,
    legacy: bool,
    previous: bool,
) -> DueOperation:
    require_exact_keys(
        record,
        (
            _LEGACY_OPERATION_KEYS
            if legacy
            else _PREVIOUS_OPERATION_KEYS
            if previous
            else _OPERATION_KEYS
        ),
    )
    if legacy:
        require_boolean(record["allow_remote_control_disconnect"])
    operation_id = OperationId(require_string(record["operation_id"]))
    kind = OperationKind(require_string(record["kind"]))
    parent_id = (
        operation_id
        if (legacy or previous) and kind.is_selection_worker
        else _optional_operation_id(record["selection_operation_id"])
    )
    return DueOperation(
        operation_id=operation_id,
        selection_operation_id=parent_id,
        provider_id=ProviderId(require_string(record["provider_id"])),
        account_id=_optional_account_id(record["account_id"]),
        kind=kind,
        priority=OperationPriority(require_string(record["priority"])),
        state=OperationState(require_string(record["state"])),
        due_at=parse_canonical_timestamp(require_string(record["due_at"])),
        updated_at=parse_canonical_timestamp(
            require_string(record["updated_at"])
        ),
        attempts=require_integer(record["attempts"]),
        failure_code=require_optional_string(record["failure_code"]),
    )


def _optional_account_id(value: JsonValue) -> SidekickAccountId | None:
    account_id = require_optional_string(value)
    return None if account_id is None else SidekickAccountId(account_id)


def _optional_operation_id(value: JsonValue) -> OperationId | None:
    operation_id = require_optional_string(value)
    return None if operation_id is None else OperationId(operation_id)


def _operation_slot(operation: DueOperation) -> str:
    owner = (
        f"provider:{operation.provider_id.value}"
        if operation.account_id is None
        else f"account:{operation.account_id}"
    )
    return f"{owner}:{operation.kind.value}"


def _operation_object(operation: DueOperation) -> JsonObject:
    return {
        "account_id": (
            None if operation.account_id is None else str(operation.account_id)
        ),
        "attempts": operation.attempts,
        "due_at": canonical_timestamp(operation.due_at),
        "failure_code": operation.failure_code,
        "kind": operation.kind.value,
        "operation_id": str(operation.operation_id),
        "priority": operation.priority.value,
        "provider_id": operation.provider_id.value,
        "selection_operation_id": (
            None
            if operation.selection_operation_id is None
            else str(operation.selection_operation_id)
        ),
        "state": operation.state.value,
        "updated_at": canonical_timestamp(operation.updated_at),
    }


def _operation_payload(document: OperationQueueDocument) -> bytes:
    operations: JsonObject = {
        _operation_slot(operation): _operation_object(operation)
        for operation in document.operations
    }
    return encode_state_object(
        {
            "operations": operations,
            "schema_version": OPERATION_QUEUE_SCHEMA_VERSION,
        },
        MAX_OPERATION_QUEUE_BYTES,
    )


def _result_object(result: WorkerResult) -> JsonObject:
    return {
        "failure_code": result.failure_code,
        "finished_at": canonical_timestamp(result.finished_at),
        "operation_id": str(result.operation_id),
        "outcome": result.outcome.value,
        "related_runtime_authority": (
            None
            if result.related_runtime_authority is None
            else _related_authority_object(result.related_runtime_authority)
        ),
        "schema_version": WORKER_RESULT_SCHEMA_VERSION,
        "selection": (
            None
            if result.selection is None
            else _selection_object(result.selection)
        ),
    }


def _result_payload(result: WorkerResult) -> bytes:
    return encode_state_object(
        _result_object(result),
        MAX_WORKER_RESULT_BYTES,
    )


def decode_worker_result(payload: bytes) -> WorkerResult:
    """Decode one canonical isolated-worker result."""
    root = decode_state_object(payload, MAX_WORKER_RESULT_BYTES)
    version = require_integer(root.get("schema_version"))
    legacy = version == _LEGACY_WORKER_RESULT_SCHEMA_VERSION
    require_exact_keys(
        root,
        _LEGACY_WORKER_RESULT_KEYS if legacy else _WORKER_RESULT_KEYS,
    )
    require_schema_version(
        version,
        (
            _LEGACY_WORKER_RESULT_SCHEMA_VERSION
            if legacy
            else WORKER_RESULT_SCHEMA_VERSION
        ),
    )
    try:
        result = WorkerResult(
            operation_id=OperationId(require_string(root["operation_id"])),
            outcome=WorkerOutcome(require_string(root["outcome"])),
            finished_at=parse_canonical_timestamp(
                require_string(root["finished_at"])
            ),
            failure_code=require_optional_string(root["failure_code"]),
            related_runtime_authority=(
                None
                if root["related_runtime_authority"] is None
                else _related_authority(
                    require_object(root["related_runtime_authority"])
                )
            ),
            selection=(
                None
                if legacy or root["selection"] is None
                else _selection(require_object(root["selection"]))
            ),
        )
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    expected = (
        _legacy_result_payload(result) if legacy else _result_payload(result)
    )
    if expected != payload:
        raise InvalidSchemaError
    return result


def encode_worker_result(result: WorkerResult) -> bytes:
    """Encode and prove one canonical isolated-worker result."""
    payload = _result_payload(result)
    if decode_worker_result(payload) != result:
        raise InvalidSchemaError
    return payload


def _related_authority(record: JsonObject) -> RelatedRuntimeAuthority:
    """Decode one provider-owned native relation proof."""
    require_exact_keys(
        record,
        {"account_id", "generation", "observed_at", "provider_id"},
    )
    return RelatedRuntimeAuthority(
        provider_id=ProviderId(require_string(record["provider_id"])),
        account_id=SidekickAccountId(require_string(record["account_id"])),
        generation=AuthorityGeneration(require_string(record["generation"])),
        observed_at=parse_canonical_timestamp(
            require_string(record["observed_at"])
        ),
    )


def _related_authority_object(
    authority: RelatedRuntimeAuthority,
) -> JsonObject:
    """Encode one provider-owned native relation proof."""
    return {
        "account_id": str(authority.account_id),
        "generation": str(authority.generation),
        "observed_at": canonical_timestamp(authority.observed_at),
        "provider_id": authority.provider_id.value,
    }


def _selection(record: JsonObject) -> SelectionWorkerMetadata:
    """Decode one safe selection-phase observation."""
    require_exact_keys(
        record,
        {
            "kind",
            "observed_account_id",
            "observed_generation",
            "operation_id",
            "pending_epoch",
            "provider_id",
        },
    )
    account = require_optional_string(record["observed_account_id"])
    generation = require_optional_string(record["observed_generation"])
    return SelectionWorkerMetadata(
        operation_id=OperationId(require_string(record["operation_id"])),
        provider_id=ProviderId(require_string(record["provider_id"])),
        kind=OperationKind(require_string(record["kind"])),
        pending_epoch=SelectionEpoch(require_integer(record["pending_epoch"])),
        observed_account_id=(
            None if account is None else SidekickAccountId(account)
        ),
        observed_generation=(
            None if generation is None else AuthorityGeneration(generation)
        ),
    )


def _selection_object(selection: SelectionWorkerMetadata) -> JsonObject:
    """Encode one safe selection-phase observation."""
    return {
        "kind": selection.kind.value,
        "observed_account_id": (
            None
            if selection.observed_account_id is None
            else str(selection.observed_account_id)
        ),
        "observed_generation": (
            None
            if selection.observed_generation is None
            else str(selection.observed_generation)
        ),
        "operation_id": str(selection.operation_id),
        "pending_epoch": selection.pending_epoch.value,
        "provider_id": selection.provider_id.value,
    }


def _legacy_result_payload(result: WorkerResult) -> bytes:
    """Re-encode one legacy result for strict migration validation."""
    if result.selection is not None:
        raise InvalidSchemaError
    record = _result_object(result)
    record.pop("selection")
    record["schema_version"] = _LEGACY_WORKER_RESULT_SCHEMA_VERSION
    return encode_state_object(record, MAX_WORKER_RESULT_BYTES)

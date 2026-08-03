"""Strict codec for secret-free global selection operations."""

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    OpenSelectionOperation,
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.models.selection import (
    MAX_SELECTION_HISTORY,
    SelectionOperationDocument,
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

SELECTION_OPERATION_SCHEMA_VERSION = 2
MAX_SELECTION_OPERATION_BYTES = 2 * 1024 * 1024

_OPEN_KEYS = frozenset(
    {
        "baseline_account_id",
        "baseline_epoch",
        "confirmed_dead_before_commit_code",
        "confirmed_dead_before_commit_count",
        "lost_after_commit_participant_ids",
        "operation_id",
        "outcome_code",
        "pending_epoch",
        "phase",
        "prepared_generation",
        "provider_id",
        "ready_participant_ids",
        "required_participant_ids",
        "started_at",
        "target_account_id",
        "target_generation",
        "updated_at",
    }
)
_RESULT_KEYS = frozenset(
    {
        "adopted_count",
        "completed_at",
        "epoch",
        "lost_count",
        "operation_id",
        "outcome",
        "provider_id",
        "ready_count",
        "required_count",
        "safe_code",
        "started_at",
        "target_account_id",
        "target_generation",
    }
)


def decode_selection_operation(
    payload: bytes,
) -> SelectionOperationDocument:
    """Decode one canonical per-provider selection journal."""
    root = decode_state_object(payload, MAX_SELECTION_OPERATION_BYTES)
    require_exact_keys(
        root,
        {"active", "history", "provider_id", "schema_version"},
    )
    require_schema_version(
        root["schema_version"],
        SELECTION_OPERATION_SCHEMA_VERSION,
    )
    history_values = require_list(root["history"])
    if len(history_values) > MAX_SELECTION_HISTORY:
        raise InvalidSchemaError
    try:
        provider_id = ProviderId(require_string(root["provider_id"]))
        active_value = root["active"]
        document = SelectionOperationDocument(
            provider_id=provider_id,
            active=(
                None
                if active_value is None
                else _open_operation(require_object(active_value))
            ),
            history=tuple(
                _selection_result(require_object(value))
                for value in history_values
            ),
        )
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    if _selection_operation_payload(document) != payload:
        raise InvalidSchemaError
    return document


def encode_selection_operation(
    document: SelectionOperationDocument,
) -> bytes:
    """Encode one canonical per-provider selection journal."""
    payload = _selection_operation_payload(document)
    if decode_selection_operation(payload) != document:
        raise InvalidSchemaError
    return payload


def _open_operation(record: JsonObject) -> OpenSelectionOperation:
    require_exact_keys(record, _OPEN_KEYS)
    return OpenSelectionOperation(
        operation_id=OperationId(require_string(record["operation_id"])),
        provider_id=ProviderId(require_string(record["provider_id"])),
        baseline_account_id=(
            None
            if (
                value := require_optional_string(record["baseline_account_id"])
            )
            is None
            else SidekickAccountId(value)
        ),
        target_account_id=SidekickAccountId(
            require_string(record["target_account_id"])
        ),
        prepared_generation=(
            None
            if (
                value := require_optional_string(record["prepared_generation"])
            )
            is None
            else AuthorityGeneration(value)
        ),
        target_generation=(
            None
            if (value := require_optional_string(record["target_generation"]))
            is None
            else AuthorityGeneration(value)
        ),
        baseline_epoch=SelectionEpoch(
            require_integer(record["baseline_epoch"])
        ),
        pending_epoch=SelectionEpoch(require_integer(record["pending_epoch"])),
        phase=SelectionPhase(require_string(record["phase"])),
        required_participant_ids=_participant_ids(
            record["required_participant_ids"]
        ),
        ready_participant_ids=_participant_ids(
            record["ready_participant_ids"]
        ),
        lost_after_commit_participant_ids=_participant_ids(
            record["lost_after_commit_participant_ids"]
        ),
        confirmed_dead_before_commit_count=require_integer(
            record["confirmed_dead_before_commit_count"]
        ),
        confirmed_dead_before_commit_code=(
            None
            if (
                value := require_optional_string(
                    record["confirmed_dead_before_commit_code"]
                )
            )
            is None
            else SelectionCode(value)
        ),
        outcome_code=(
            None
            if (value := require_optional_string(record["outcome_code"]))
            is None
            else SelectionCode(value)
        ),
        started_at=parse_canonical_timestamp(
            require_string(record["started_at"])
        ),
        updated_at=parse_canonical_timestamp(
            require_string(record["updated_at"])
        ),
    )


def _selection_result(record: JsonObject) -> SelectionResult:
    require_exact_keys(record, _RESULT_KEYS)
    return SelectionResult(
        operation_id=OperationId(require_string(record["operation_id"])),
        provider_id=ProviderId(require_string(record["provider_id"])),
        target_account_id=SidekickAccountId(
            require_string(record["target_account_id"])
        ),
        target_generation=(
            None
            if (value := require_optional_string(record["target_generation"]))
            is None
            else AuthorityGeneration(value)
        ),
        epoch=SelectionEpoch(require_integer(record["epoch"])),
        outcome=SelectionOutcome(require_string(record["outcome"])),
        safe_code=SelectionCode(require_string(record["safe_code"])),
        required_count=require_integer(record["required_count"]),
        ready_count=require_integer(record["ready_count"]),
        adopted_count=require_integer(record["adopted_count"]),
        lost_count=require_integer(record["lost_count"]),
        started_at=parse_canonical_timestamp(
            require_string(record["started_at"])
        ),
        completed_at=parse_canonical_timestamp(
            require_string(record["completed_at"])
        ),
    )


def _participant_ids(value: JsonValue) -> tuple[ParticipantId, ...]:
    return tuple(
        ParticipantId(require_string(item)) for item in require_list(value)
    )


def _open_object(operation: OpenSelectionOperation) -> JsonObject:
    return {
        "baseline_account_id": (
            None
            if operation.baseline_account_id is None
            else str(operation.baseline_account_id)
        ),
        "baseline_epoch": operation.baseline_epoch.value,
        "confirmed_dead_before_commit_code": (
            None
            if operation.confirmed_dead_before_commit_code is None
            else operation.confirmed_dead_before_commit_code.value
        ),
        "confirmed_dead_before_commit_count": (
            operation.confirmed_dead_before_commit_count
        ),
        "lost_after_commit_participant_ids": [
            str(participant_id)
            for participant_id in (operation.lost_after_commit_participant_ids)
        ],
        "operation_id": str(operation.operation_id),
        "outcome_code": (
            None
            if operation.outcome_code is None
            else operation.outcome_code.value
        ),
        "pending_epoch": operation.pending_epoch.value,
        "phase": operation.phase.value,
        "prepared_generation": (
            None
            if operation.prepared_generation is None
            else str(operation.prepared_generation)
        ),
        "provider_id": operation.provider_id.value,
        "ready_participant_ids": [
            str(participant_id)
            for participant_id in operation.ready_participant_ids
        ],
        "required_participant_ids": [
            str(participant_id)
            for participant_id in operation.required_participant_ids
        ],
        "started_at": canonical_timestamp(operation.started_at),
        "target_account_id": str(operation.target_account_id),
        "target_generation": (
            None
            if operation.target_generation is None
            else str(operation.target_generation)
        ),
        "updated_at": canonical_timestamp(operation.updated_at),
    }


def _result_object(result: SelectionResult) -> JsonObject:
    return {
        "adopted_count": result.adopted_count,
        "completed_at": canonical_timestamp(result.completed_at),
        "epoch": result.epoch.value,
        "lost_count": result.lost_count,
        "operation_id": str(result.operation_id),
        "outcome": result.outcome.value,
        "provider_id": result.provider_id.value,
        "ready_count": result.ready_count,
        "required_count": result.required_count,
        "safe_code": result.safe_code.value,
        "started_at": canonical_timestamp(result.started_at),
        "target_account_id": str(result.target_account_id),
        "target_generation": (
            None
            if result.target_generation is None
            else str(result.target_generation)
        ),
    }


def _selection_operation_payload(
    document: SelectionOperationDocument,
) -> bytes:
    return encode_state_object(
        {
            "active": (
                None
                if document.active is None
                else _open_object(document.active)
            ),
            "history": [_result_object(result) for result in document.history],
            "provider_id": document.provider_id.value,
            "schema_version": SELECTION_OPERATION_SCHEMA_VERSION,
        },
        MAX_SELECTION_OPERATION_BYTES,
    )

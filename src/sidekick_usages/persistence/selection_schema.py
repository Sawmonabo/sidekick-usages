"""Strict no-secret codecs for selection and supervisor operation state."""

import json
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

from sidekick_usages.core.accounts import (
    AuthorityGeneration,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection import (
    ActivationOutcome,
    ActivationPhase,
    ActivationRecord,
    DueOperation,
    OperationKind,
    OperationPriority,
    OperationState,
    ProviderRuntimeState,
    SelectedAccountState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import (
    DuplicateKeyError,
    InvalidSchemaError,
    MalformedJsonError,
)
from sidekick_usages.persistence.schemas import (
    MAX_ACCOUNTS,
    MAX_DOCUMENT_BYTES,
    _canonical_timestamp,
    _parse_canonical_timestamp,
)
from sidekick_usages.persistence.state_validation import (
    validate_non_secret_state,
)
from sidekick_usages.serialization import (
    JsonDecodeCode,
    JsonDecodeError,
    JsonObject,
    decode_json_value,
)

STATE_SCHEMA_VERSION = 1
MAX_ACTIVATION_HISTORY = 32
MAX_SELECTED_STATE_BYTES = 256 * 1024
MAX_ACTIVATION_JOURNAL_BYTES = 512 * 1024
MAX_OPERATION_QUEUE_BYTES = 8 * 1024 * 1024
MAX_OPERATION_RECORDS = MAX_ACCOUNTS * len(OperationKind)

_MAX_METADATA_BYTES = 4_096
_MAX_SAFE_CODE_BYTES = 128
_MAX_OPERATION_ATTEMPTS = 1_000_000
_MODEL_CONFIG = ConfigDict(strict=True, extra="forbid", frozen=True)

type _ProviderName = Literal["claude", "codex"]
type _RuntimeStateName = Literal[
    "saved_active",
    "external_active",
    "logged_out",
    "unreadable",
    "unsupported",
]
type _ActivationPhaseName = Literal[
    "prepared",
    "outgoing_retained",
    "target_activated",
    "read_back_verified",
    "committed",
    "rolled_back",
    "reconciliation_required",
]
type _ActivationOutcomeName = Literal[
    "verified",
    "rolled_back",
    "external_reconciled",
    "logged_out",
    "reconciliation_required",
    "unsupported",
]
type _OperationKindName = Literal[
    "maintain",
    "refresh",
    "usage",
    "activity",
    "login",
    "migrate",
    "activate",
    "repair",
    "reconcile",
]
type _OperationPriorityName = Literal[
    "codex_callback",
    "interactive",
    "scheduled",
]
type _OperationStateName = Literal[
    "scheduled",
    "running",
    "retry_wait",
    "action_required",
]


def _canonical_uuid(value: str) -> str:
    """Require one canonical Sidekick-style UUID."""
    SidekickAccountId(value)
    return value


def _canonical_time(value: str) -> str:
    """Require one canonical aware UTC timestamp."""
    _parse_canonical_timestamp(value)
    return value


def _bounded_text(value: str) -> str:
    """Require one bounded nonempty UTF-8 metadata value."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Metadata must be valid UTF-8.") from None
    if not encoded or len(encoded) > _MAX_METADATA_BYTES:
        raise ValueError("Metadata must be nonempty and bounded.")
    return value


def _safe_code(value: str) -> str:
    """Require one bounded lowercase ASCII result code."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Safe result code must be valid UTF-8.") from None
    if (
        not encoded
        or len(encoded) > _MAX_SAFE_CODE_BYTES
        or not all(
            character.isascii()
            and (
                character.islower() or character.isdigit() or character == "_"
            )
            for character in value
        )
    ):
        raise ValueError("Safe result code has an invalid format.")
    return value


def _selected_records(
    value: dict[_ProviderName, _SelectedRecordModel],
) -> dict[_ProviderName, _SelectedRecordModel]:
    if len(value) > len(ProviderId):
        raise ValueError("Selected state has too many providers.")
    return value


def _operation_records(
    value: dict[str, _OperationRecordModel],
) -> dict[str, _OperationRecordModel]:
    if len(value) > MAX_OPERATION_RECORDS:
        raise ValueError("Operation queue has too many records.")
    return value


def _operation_slot(value: str) -> str:
    account_id, separator, kind = value.partition(":")
    if separator != ":":
        raise ValueError("Operation slot is malformed.")
    SidekickAccountId(account_id)
    OperationKind(kind)
    return value


type _Uuid = Annotated[str, AfterValidator(_canonical_uuid)]
type _Timestamp = Annotated[str, AfterValidator(_canonical_time)]
type _Metadata = Annotated[str, AfterValidator(_bounded_text)]
type _SafeCode = Annotated[str, AfterValidator(_safe_code)]
type _OperationSlot = Annotated[str, AfterValidator(_operation_slot)]


class _SelectedRecordModel(BaseModel):
    """Strict persisted provider read-back record."""

    model_config = _MODEL_CONFIG

    runtime_state: _RuntimeStateName
    account_id: _Uuid | None
    provider_identity: _Metadata | None
    runtime_generation: _Metadata | None
    verified_at: _Timestamp
    outcome: _ActivationOutcomeName


type _SelectedRecords = Annotated[
    dict[_ProviderName, _SelectedRecordModel],
    AfterValidator(_selected_records),
]


class _SelectedDocumentModel(BaseModel):
    """Strict selected-state document."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1]
    providers: _SelectedRecords


class _ActivationRecordModel(BaseModel):
    """Strict persisted activation record."""

    model_config = _MODEL_CONFIG

    provider_id: _ProviderName
    operation_id: _Uuid
    source_account_id: _Uuid | None
    target_account_id: _Uuid
    source_provider_identity: _Metadata | None
    source_generation: _Metadata | None
    expected_target_identity: _Metadata
    phase: _ActivationPhaseName
    started_at: _Timestamp
    updated_at: _Timestamp
    outcome: _ActivationOutcomeName | None
    failure_code: _SafeCode | None


class _ActivationDocumentModel(BaseModel):
    """Strict one-provider activation journal."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1]
    provider_id: _ProviderName
    active: _ActivationRecordModel | None
    history: list[_ActivationRecordModel] = Field(
        max_length=MAX_ACTIVATION_HISTORY
    )


class _OperationRecordModel(BaseModel):
    """Strict persisted account-operation slot."""

    model_config = _MODEL_CONFIG

    operation_id: _Uuid
    provider_id: _ProviderName
    account_id: _Uuid
    kind: _OperationKindName
    priority: _OperationPriorityName
    state: _OperationStateName
    due_at: _Timestamp
    updated_at: _Timestamp
    attempts: int = Field(ge=0, le=_MAX_OPERATION_ATTEMPTS)
    failure_code: _SafeCode | None


type _OperationRecords = Annotated[
    dict[_OperationSlot, _OperationRecordModel],
    AfterValidator(_operation_records),
]


class _OperationDocumentModel(BaseModel):
    """Strict durable operation queue document."""

    model_config = _MODEL_CONFIG

    schema_version: Literal[1]
    operations: _OperationRecords


_SELECTED_ADAPTER = TypeAdapter(_SelectedDocumentModel)
_ACTIVATION_ADAPTER = TypeAdapter(_ActivationDocumentModel)
_OPERATION_ADAPTER = TypeAdapter(_OperationDocumentModel)


@dataclass(frozen=True, slots=True)
class SelectedStateDocument:
    """Validated selected states in deterministic provider order."""

    states: tuple[SelectedAccountState, ...] = ()

    def __post_init__(self) -> None:
        """Reject duplicate providers and normalize ordering."""
        providers = {state.provider_id for state in self.states}
        if len(providers) != len(self.states):
            raise InvalidSchemaError
        object.__setattr__(
            self,
            "states",
            tuple(
                sorted(
                    self.states,
                    key=lambda state: state.provider_id.value,
                )
            ),
        )

    def get(self, provider_id: ProviderId) -> SelectedAccountState | None:
        """Return one provider state when present."""
        return next(
            (
                state
                for state in self.states
                if state.provider_id is provider_id
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class ActivationJournalDocument:
    """One active activation plus bounded terminal history."""

    provider_id: ProviderId
    active: ActivationRecord | None = None
    history: tuple[ActivationRecord, ...] = ()

    def __post_init__(self) -> None:
        """Require provider ownership and active/terminal separation."""
        if len(self.history) > MAX_ACTIVATION_HISTORY:
            raise InvalidSchemaError
        records = (
            self.history
            if self.active is None
            else (*self.history, self.active)
        )
        if any(
            record.provider_id is not self.provider_id for record in records
        ):
            raise InvalidSchemaError
        operation_ids = {record.operation_id for record in records}
        if len(operation_ids) != len(records):
            raise InvalidSchemaError
        if self.active is not None and self.active.phase.terminal:
            raise InvalidSchemaError
        if any(not record.phase.terminal for record in self.history):
            raise InvalidSchemaError


@dataclass(frozen=True, slots=True)
class OperationQueueDocument:
    """Validated operation slots in deterministic key order."""

    operations: tuple[DueOperation, ...] = ()

    def __post_init__(self) -> None:
        """Reject duplicate slots or operation identifiers."""
        if len(self.operations) > MAX_OPERATION_RECORDS:
            raise InvalidSchemaError
        slots = {
            (operation.account_id, operation.kind)
            for operation in self.operations
        }
        operation_ids = {
            operation.operation_id for operation in self.operations
        }
        if len(slots) != len(self.operations) or len(operation_ids) != len(
            self.operations
        ):
            raise InvalidSchemaError
        object.__setattr__(
            self,
            "operations",
            tuple(
                sorted(
                    self.operations,
                    key=lambda operation: (
                        str(operation.account_id),
                        operation.kind.value,
                    ),
                )
            ),
        )


def _decode_root(payload: bytes, maximum: int) -> JsonObject:
    """Decode one bounded strict no-secret JSON object."""
    if len(payload) > maximum or len(payload) > MAX_DOCUMENT_BYTES:
        raise InvalidSchemaError
    try:
        root = decode_json_value(payload)
    except JsonDecodeError as error:
        if error.code is JsonDecodeCode.DUPLICATE_KEY:
            raise DuplicateKeyError from None
        raise MalformedJsonError from None
    if not isinstance(root, dict):
        raise InvalidSchemaError
    validate_non_secret_state(root)
    return root


def _dump(root: JsonObject, maximum: int) -> bytes:
    """Encode one bounded canonical no-secret JSON document."""
    validate_non_secret_state(root)
    try:
        payload = (
            json.dumps(
                root,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except TypeError, ValueError, UnicodeEncodeError:
        raise InvalidSchemaError from None
    if len(payload) > maximum or len(payload) > MAX_DOCUMENT_BYTES:
        raise InvalidSchemaError
    return payload


def _optional_identity(value: str | None) -> ProviderIdentity | None:
    return None if value is None else ProviderIdentity(value)


def _optional_generation(
    value: str | None,
) -> AuthorityGeneration | None:
    return None if value is None else AuthorityGeneration(value)


def _selected_state(
    provider_id: str,
    model: _SelectedRecordModel,
) -> SelectedAccountState:
    """Convert one validated selected-state model."""
    return SelectedAccountState(
        provider_id=ProviderId(provider_id),
        runtime_state=ProviderRuntimeState(model.runtime_state),
        account_id=(
            None
            if model.account_id is None
            else SidekickAccountId(model.account_id)
        ),
        provider_identity=_optional_identity(model.provider_identity),
        runtime_generation=_optional_generation(model.runtime_generation),
        verified_at=_parse_canonical_timestamp(model.verified_at),
        outcome=ActivationOutcome(model.outcome),
    )


def _selected_object(state: SelectedAccountState) -> JsonObject:
    """Encode one selected-state record."""
    return {
        "runtime_state": state.runtime_state.value,
        "account_id": (
            None if state.account_id is None else str(state.account_id)
        ),
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
        "verified_at": _canonical_timestamp(state.verified_at),
        "outcome": state.outcome.value,
    }


def _selected_payload(document: SelectedStateDocument) -> bytes:
    providers: JsonObject = {
        state.provider_id.value: _selected_object(state)
        for state in document.states
    }
    return _dump(
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "providers": providers,
        },
        MAX_SELECTED_STATE_BYTES,
    )


def decode_selected_state(payload: bytes) -> SelectedStateDocument:
    """Decode one canonical selected-state document."""
    root = _decode_root(payload, MAX_SELECTED_STATE_BYTES)
    try:
        model = _SELECTED_ADAPTER.validate_python(root, strict=True)
        document = SelectedStateDocument(
            tuple(
                _selected_state(provider_id, state)
                for provider_id, state in model.providers.items()
            )
        )
    except ValidationError, TypeError, ValueError:
        raise InvalidSchemaError from None
    if _selected_payload(document) != payload:
        raise InvalidSchemaError
    return document


def encode_selected_state(document: SelectedStateDocument) -> bytes:
    """Encode one canonical selected-state document."""
    payload = _selected_payload(document)
    if decode_selected_state(payload) != document:
        raise InvalidSchemaError
    return payload


def _activation_record(
    model: _ActivationRecordModel,
) -> ActivationRecord:
    """Convert one validated activation record."""
    return ActivationRecord(
        provider_id=ProviderId(model.provider_id),
        operation_id=OperationId(model.operation_id),
        source_account_id=(
            None
            if model.source_account_id is None
            else SidekickAccountId(model.source_account_id)
        ),
        target_account_id=SidekickAccountId(model.target_account_id),
        source_provider_identity=_optional_identity(
            model.source_provider_identity
        ),
        source_generation=_optional_generation(model.source_generation),
        expected_target_identity=ProviderIdentity(
            model.expected_target_identity
        ),
        phase=ActivationPhase(model.phase),
        started_at=_parse_canonical_timestamp(model.started_at),
        updated_at=_parse_canonical_timestamp(model.updated_at),
        outcome=(
            None if model.outcome is None else ActivationOutcome(model.outcome)
        ),
        failure_code=model.failure_code,
    )


def _activation_object(record: ActivationRecord) -> JsonObject:
    """Encode one activation journal record."""
    return {
        "provider_id": record.provider_id.value,
        "operation_id": str(record.operation_id),
        "source_account_id": (
            None
            if record.source_account_id is None
            else str(record.source_account_id)
        ),
        "target_account_id": str(record.target_account_id),
        "source_provider_identity": (
            None
            if record.source_provider_identity is None
            else str(record.source_provider_identity)
        ),
        "source_generation": (
            None
            if record.source_generation is None
            else str(record.source_generation)
        ),
        "expected_target_identity": str(record.expected_target_identity),
        "phase": record.phase.value,
        "started_at": _canonical_timestamp(record.started_at),
        "updated_at": _canonical_timestamp(record.updated_at),
        "outcome": (None if record.outcome is None else record.outcome.value),
        "failure_code": record.failure_code,
    }


def _activation_payload(document: ActivationJournalDocument) -> bytes:
    return _dump(
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "provider_id": document.provider_id.value,
            "active": (
                None
                if document.active is None
                else _activation_object(document.active)
            ),
            "history": [
                _activation_object(record) for record in document.history
            ],
        },
        MAX_ACTIVATION_JOURNAL_BYTES,
    )


def decode_activation_journal(
    payload: bytes,
) -> ActivationJournalDocument:
    """Decode one canonical provider activation journal."""
    root = _decode_root(payload, MAX_ACTIVATION_JOURNAL_BYTES)
    try:
        model = _ACTIVATION_ADAPTER.validate_python(root, strict=True)
        document = ActivationJournalDocument(
            provider_id=ProviderId(model.provider_id),
            active=(
                None
                if model.active is None
                else _activation_record(model.active)
            ),
            history=tuple(
                _activation_record(record) for record in model.history
            ),
        )
    except ValidationError, TypeError, ValueError:
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


def _due_operation(model: _OperationRecordModel) -> DueOperation:
    """Convert one validated durable operation record."""
    return DueOperation(
        operation_id=OperationId(model.operation_id),
        provider_id=ProviderId(model.provider_id),
        account_id=SidekickAccountId(model.account_id),
        kind=OperationKind(model.kind),
        priority=OperationPriority(model.priority),
        state=OperationState(model.state),
        due_at=_parse_canonical_timestamp(model.due_at),
        updated_at=_parse_canonical_timestamp(model.updated_at),
        attempts=model.attempts,
        failure_code=model.failure_code,
    )


def _operation_object(operation: DueOperation) -> JsonObject:
    """Encode one durable operation record."""
    return {
        "operation_id": str(operation.operation_id),
        "provider_id": operation.provider_id.value,
        "account_id": str(operation.account_id),
        "kind": operation.kind.value,
        "priority": operation.priority.value,
        "state": operation.state.value,
        "due_at": _canonical_timestamp(operation.due_at),
        "updated_at": _canonical_timestamp(operation.updated_at),
        "attempts": operation.attempts,
        "failure_code": operation.failure_code,
    }


def _operation_payload(document: OperationQueueDocument) -> bytes:
    operations: JsonObject = {
        operation_slot(operation.account_id, operation.kind): (
            _operation_object(operation)
        )
        for operation in document.operations
    }
    return _dump(
        {
            "schema_version": STATE_SCHEMA_VERSION,
            "operations": operations,
        },
        MAX_OPERATION_QUEUE_BYTES,
    )


def decode_operation_queue(payload: bytes) -> OperationQueueDocument:
    """Decode one canonical durable operation queue."""
    root = _decode_root(payload, MAX_OPERATION_QUEUE_BYTES)
    try:
        model = _OPERATION_ADAPTER.validate_python(root, strict=True)
        operations: list[DueOperation] = []
        for slot, record in model.operations.items():
            operation = _due_operation(record)
            if slot != operation_slot(operation.account_id, operation.kind):
                raise ValueError("Operation slot and record disagree.")
            operations.append(operation)
        document = OperationQueueDocument(tuple(operations))
    except ValidationError, TypeError, ValueError:
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


__all__ = [
    "MAX_ACTIVATION_HISTORY",
    "MAX_ACTIVATION_JOURNAL_BYTES",
    "MAX_OPERATION_QUEUE_BYTES",
    "MAX_OPERATION_RECORDS",
    "MAX_SELECTED_STATE_BYTES",
    "STATE_SCHEMA_VERSION",
    "ActivationJournalDocument",
    "OperationQueueDocument",
    "SelectedStateDocument",
    "decode_activation_journal",
    "decode_operation_queue",
    "decode_selected_state",
    "encode_activation_journal",
    "encode_operation_queue",
    "encode_selected_state",
    "operation_slot",
]

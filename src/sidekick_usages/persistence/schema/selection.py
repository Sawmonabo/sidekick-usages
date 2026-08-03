"""Strict lightweight codecs for selection and durable operation state."""

from decimal import Decimal, InvalidOperation

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    CredentialAction,
    CredentialHealth,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    ClaudeAuthObservation,
    DueOperation,
    FinalizedSelection,
    ProviderAuthObservation,
    SelectedAccountState,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    OperationKind,
    OperationPriority,
    OperationState,
    ProviderAuthState,
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
    require_boolean,
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

SELECTED_STATE_SCHEMA_VERSION = 3
_LEGACY_SELECTED_STATE_SCHEMA_VERSION = 2
ACTIVATION_SCHEMA_VERSION = 4
OPERATION_QUEUE_SCHEMA_VERSION = 4
_LEGACY_OPERATION_QUEUE_SCHEMA_VERSION = 3
RUNTIME_OBSERVATION_SCHEMA_VERSION = 1
MAX_SELECTED_STATE_BYTES = 256 * 1024
MAX_ACTIVATION_JOURNAL_BYTES = 512 * 1024
MAX_OPERATION_QUEUE_BYTES = 8 * 1024 * 1024
MAX_RUNTIME_OBSERVATION_BYTES = 256 * 1024
MAX_CLAUDE_MTIME_MILLISECONDS_BYTES = 32

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
_FINALIZED_SELECTION_KEYS = frozenset(
    {
        "account_id",
        "epoch",
        "finalized_at",
        "generation",
    }
)
_ACTIVATION_KEYS = frozenset(
    {
        "expected_target_identity",
        "failure_code",
        "native_auth_baseline",
        "operation_id",
        "outcome",
        "phase",
        "provider_id",
        "reconciliation_origin_phase",
        "selected_baseline",
        "started_at",
        "target_authority_generation",
        "target_account_id",
        "updated_at",
        "verified_runtime_generation",
    }
)
_CLAUDE_AUTH_KEYS = frozenset(
    {
        "access_expires_at",
        "action",
        "generation",
        "health",
        "modified_milliseconds",
        "observed_at",
        "plan",
        "provider_identity",
        "refresh_expires_at",
        "scopes",
        "state",
    }
)
_PROVIDER_AUTH_KEYS = frozenset(
    {
        "generation",
        "observed_at",
        "provider_identity",
        "state",
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
_LEGACY_OPERATION_KEYS = _OPERATION_KEYS | {"allow_remote_control_disconnect"}


def decode_selected_state(payload: bytes) -> SelectedStateDocument:
    """Decode one canonical provider selected-state document."""
    root = decode_state_object(payload, MAX_SELECTED_STATE_BYTES)
    require_exact_keys(root, {"providers", "schema_version"})
    require_schema_version(
        root["schema_version"],
        SELECTED_STATE_SCHEMA_VERSION,
    )
    providers = require_object(root["providers"])
    if len(providers) > len(ProviderId):
        raise InvalidSchemaError
    try:
        states = tuple(
            _finalized_selection(ProviderId(name), require_object(value))
            for name, value in providers.items()
        )
        document = SelectedStateDocument(states)
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    if _selected_payload(document) != payload:
        raise InvalidSchemaError
    return document


def migrate_selected_state_version_two(
    payload: bytes,
) -> SelectedStateDocument:
    """Validate v2 and retain only saved-active selections at epoch zero."""
    root = decode_state_object(payload, MAX_SELECTED_STATE_BYTES)
    require_exact_keys(root, {"providers", "schema_version"})
    require_schema_version(
        root["schema_version"],
        _LEGACY_SELECTED_STATE_SCHEMA_VERSION,
    )
    providers = require_object(root["providers"])
    if len(providers) > len(ProviderId):
        raise InvalidSchemaError
    try:
        legacy_states = tuple(
            _selected_state(ProviderId(name), require_object(value))
            for name, value in providers.items()
        )
        migrated = SelectedStateDocument(
            tuple(
                FinalizedSelection(
                    provider_id=state.provider_id,
                    account_id=state.account_id,
                    epoch=SelectionEpoch(0),
                    generation=state.runtime_generation,
                    finalized_at=state.verified_at,
                )
                for state in legacy_states
                if state.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
                and state.account_id is not None
                and state.runtime_generation is not None
            )
        )
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    if _legacy_selected_payload(legacy_states) != payload:
        raise InvalidSchemaError
    return migrated


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
    require_schema_version(
        root["schema_version"],
        ACTIVATION_SCHEMA_VERSION,
    )
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
    provider_id: ProviderId,
    account_id: SidekickAccountId | None,
    kind: OperationKind,
) -> str:
    """Return one canonical durable account or provider operation slot."""
    owner = (
        f"provider:{provider_id.value}"
        if account_id is None
        else f"account:{account_id}"
    )
    return f"{owner}:{kind.value}"


def decode_operation_queue(payload: bytes) -> OperationQueueDocument:
    """Decode one canonical durable operation queue."""
    root = decode_state_object(payload, MAX_OPERATION_QUEUE_BYTES)
    require_exact_keys(root, {"operations", "schema_version"})
    version = require_integer(root["schema_version"])
    legacy = version == _LEGACY_OPERATION_QUEUE_SCHEMA_VERSION
    require_schema_version(
        version,
        (
            _LEGACY_OPERATION_QUEUE_SCHEMA_VERSION
            if legacy
            else OPERATION_QUEUE_SCHEMA_VERSION
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
            )
            if slot != operation_slot(
                operation.provider_id,
                operation.account_id,
                operation.kind,
            ):
                raise InvalidSchemaError
            operations.append(operation)
        document = OperationQueueDocument(tuple(operations))
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    expected = (
        encode_state_object(root, MAX_OPERATION_QUEUE_BYTES)
        if legacy
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


def decode_runtime_auth_observation(
    payload: bytes,
) -> ProviderAuthObservation:
    """Decode one canonical provider runtime-auth observation."""
    root = decode_state_object(payload, MAX_RUNTIME_OBSERVATION_BYTES)
    require_exact_keys(
        root,
        {"observation", "provider_id", "schema_version"},
    )
    require_schema_version(
        root["schema_version"],
        RUNTIME_OBSERVATION_SCHEMA_VERSION,
    )
    provider_id = ProviderId(require_string(root["provider_id"]))
    try:
        observation = _provider_auth_observation(
            provider_id,
            require_object(root["observation"]),
        )
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    if encode_runtime_auth_observation(observation) != payload:
        raise InvalidSchemaError
    return observation


def encode_runtime_auth_observation(
    observation: ProviderAuthObservation,
) -> bytes:
    """Encode one canonical provider runtime-auth observation."""
    return encode_state_object(
        {
            "observation": _provider_auth_object(observation),
            "provider_id": observation.provider_id.value,
            "schema_version": RUNTIME_OBSERVATION_SCHEMA_VERSION,
        },
        MAX_RUNTIME_OBSERVATION_BYTES,
    )


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


def _finalized_selection(
    provider_id: ProviderId,
    record: JsonObject,
) -> FinalizedSelection:
    require_exact_keys(record, _FINALIZED_SELECTION_KEYS)
    return FinalizedSelection(
        provider_id=provider_id,
        account_id=SidekickAccountId(require_string(record["account_id"])),
        epoch=SelectionEpoch(require_integer(record["epoch"])),
        generation=AuthorityGeneration(require_string(record["generation"])),
        finalized_at=parse_canonical_timestamp(
            require_string(record["finalized_at"])
        ),
    )


def _finalized_object(selection: FinalizedSelection) -> JsonObject:
    return {
        "account_id": str(selection.account_id),
        "epoch": selection.epoch.value,
        "finalized_at": canonical_timestamp(selection.finalized_at),
        "generation": str(selection.generation),
    }


def _selected_payload(document: SelectedStateDocument) -> bytes:
    providers: JsonObject = {
        state.provider_id.value: _finalized_object(state)
        for state in document.states
    }
    return encode_state_object(
        {
            "providers": providers,
            "schema_version": SELECTED_STATE_SCHEMA_VERSION,
        },
        MAX_SELECTED_STATE_BYTES,
    )


def _legacy_selected_payload(
    states: tuple[SelectedAccountState, ...],
) -> bytes:
    providers: JsonObject = {
        state.provider_id.value: _selected_object(state) for state in states
    }
    return encode_state_object(
        {
            "providers": providers,
            "schema_version": _LEGACY_SELECTED_STATE_SCHEMA_VERSION,
        },
        MAX_SELECTED_STATE_BYTES,
    )


def _activation_record(record: JsonObject) -> ActivationRecord:
    require_exact_keys(record, _ACTIVATION_KEYS)
    provider_id = ProviderId(require_string(record["provider_id"]))
    selected_value = record["selected_baseline"]
    outcome = require_optional_string(record["outcome"])
    reconciliation_origin = require_optional_string(
        record["reconciliation_origin_phase"]
    )
    verified_generation = require_optional_string(
        record["verified_runtime_generation"]
    )
    return ActivationRecord(
        provider_id=provider_id,
        operation_id=OperationId(require_string(record["operation_id"])),
        selected_baseline=(
            None
            if selected_value is None
            else _selected_state(
                provider_id,
                require_object(selected_value),
            )
        ),
        native_auth_baseline=_activation_auth_observation(
            provider_id,
            require_object(record["native_auth_baseline"]),
        ),
        target_account_id=SidekickAccountId(
            require_string(record["target_account_id"])
        ),
        expected_target_identity=ProviderIdentity(
            require_string(record["expected_target_identity"])
        ),
        target_authority_generation=AuthorityGeneration(
            require_string(record["target_authority_generation"])
        ),
        phase=ActivationPhase(require_string(record["phase"])),
        started_at=parse_canonical_timestamp(
            require_string(record["started_at"])
        ),
        updated_at=parse_canonical_timestamp(
            require_string(record["updated_at"])
        ),
        verified_runtime_generation=(
            None
            if verified_generation is None
            else AuthorityGeneration(verified_generation)
        ),
        outcome=(None if outcome is None else ActivationOutcome(outcome)),
        failure_code=require_optional_string(record["failure_code"]),
        reconciliation_origin_phase=(
            None
            if reconciliation_origin is None
            else ActivationPhase(reconciliation_origin)
        ),
    )


def _activation_object(record: ActivationRecord) -> JsonObject:
    return {
        "expected_target_identity": str(record.expected_target_identity),
        "failure_code": record.failure_code,
        "native_auth_baseline": _activation_auth_object(
            record.provider_id,
            record.native_auth_baseline,
        ),
        "operation_id": str(record.operation_id),
        "outcome": (None if record.outcome is None else record.outcome.value),
        "phase": record.phase.value,
        "provider_id": record.provider_id.value,
        "reconciliation_origin_phase": (
            None
            if record.reconciliation_origin_phase is None
            else record.reconciliation_origin_phase.value
        ),
        "selected_baseline": (
            None
            if record.selected_baseline is None
            else _selected_object(record.selected_baseline)
        ),
        "started_at": canonical_timestamp(record.started_at),
        "target_authority_generation": str(record.target_authority_generation),
        "target_account_id": str(record.target_account_id),
        "updated_at": canonical_timestamp(record.updated_at),
        "verified_runtime_generation": (
            None
            if record.verified_runtime_generation is None
            else str(record.verified_runtime_generation)
        ),
    }


def _activation_auth_observation(
    provider_id: ProviderId,
    record: JsonObject,
) -> ProviderAuthObservation:
    if provider_id is not ProviderId.CLAUDE:
        return _provider_auth_observation(provider_id, record)
    state = ProviderAuthState(require_string(record["state"]))
    if state is ProviderAuthState.LOGGED_OUT:
        return _provider_auth_observation(provider_id, record)
    require_exact_keys(record, _CLAUDE_AUTH_KEYS)
    identity = require_string(record["provider_identity"])
    generation = require_string(record["generation"])
    scopes = require_list(record["scopes"])
    refresh_expires_at = require_optional_string(record["refresh_expires_at"])
    return ClaudeAuthObservation(
        provider_id=provider_id,
        state=state,
        provider_identity=ProviderIdentity(identity),
        generation=AuthorityGeneration(generation),
        observed_at=parse_canonical_timestamp(
            require_string(record["observed_at"])
        ),
        plan=require_string(record["plan"]),
        scopes=tuple(require_string(scope) for scope in scopes),
        access_expires_at=parse_canonical_timestamp(
            require_string(record["access_expires_at"])
        ),
        refresh_expires_at=(
            None
            if refresh_expires_at is None
            else parse_canonical_timestamp(refresh_expires_at)
        ),
        health=CredentialHealth(require_string(record["health"])),
        action=CredentialAction(require_string(record["action"])),
        modified_milliseconds=_modified_milliseconds(
            record["modified_milliseconds"]
        ),
    )


def _activation_auth_object(
    provider_id: ProviderId,
    observation: ProviderAuthObservation,
) -> JsonObject:
    if provider_id is not ProviderId.CLAUDE:
        return _provider_auth_object(observation)
    if (
        type(observation) is ProviderAuthObservation
        and observation.state is ProviderAuthState.LOGGED_OUT
    ):
        return _provider_auth_object(observation)
    if not isinstance(observation, ClaudeAuthObservation):
        raise InvalidSchemaError
    return {
        "access_expires_at": canonical_timestamp(
            observation.access_expires_at
        ),
        "action": observation.action.value,
        "generation": str(observation.generation),
        "health": observation.health.value,
        "modified_milliseconds": (
            None
            if observation.modified_milliseconds is None
            else _canonical_decimal(observation.modified_milliseconds)
        ),
        "observed_at": canonical_timestamp(observation.observed_at),
        "plan": observation.plan,
        "provider_identity": str(observation.provider_identity),
        "refresh_expires_at": (
            None
            if observation.refresh_expires_at is None
            else canonical_timestamp(observation.refresh_expires_at)
        ),
        "scopes": list(observation.scopes),
        "state": observation.state.value,
    }


def _modified_milliseconds(value: JsonValue) -> Decimal | None:
    text = require_optional_string(value)
    if text is None:
        return None
    if len(text.encode("utf-8")) > MAX_CLAUDE_MTIME_MILLISECONDS_BYTES:
        raise InvalidSchemaError
    try:
        modified = Decimal(text)
    except InvalidOperation:
        raise InvalidSchemaError from None
    if (
        not modified.is_finite()
        or modified < 0
        or _canonical_decimal(modified) != text
    ):
        raise InvalidSchemaError
    return modified


def _canonical_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized.is_zero():
        return "0"
    return format(normalized, "f")


def _provider_auth_observation(
    provider_id: ProviderId,
    record: JsonObject,
) -> ProviderAuthObservation:
    require_exact_keys(record, _PROVIDER_AUTH_KEYS)
    identity = require_optional_string(record["provider_identity"])
    generation = require_optional_string(record["generation"])
    return ProviderAuthObservation(
        provider_id=provider_id,
        state=ProviderAuthState(require_string(record["state"])),
        provider_identity=(
            None if identity is None else ProviderIdentity(identity)
        ),
        generation=(
            None if generation is None else AuthorityGeneration(generation)
        ),
        observed_at=parse_canonical_timestamp(
            require_string(record["observed_at"])
        ),
    )


def _provider_auth_object(
    observation: ProviderAuthObservation,
) -> JsonObject:
    return {
        "generation": (
            None
            if observation.generation is None
            else str(observation.generation)
        ),
        "observed_at": canonical_timestamp(observation.observed_at),
        "provider_identity": (
            None
            if observation.provider_identity is None
            else str(observation.provider_identity)
        ),
        "state": observation.state.value,
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
            "schema_version": ACTIVATION_SCHEMA_VERSION,
        },
        MAX_ACTIVATION_JOURNAL_BYTES,
    )


def _due_operation(
    record: JsonObject,
    *,
    legacy: bool,
) -> DueOperation:
    require_exact_keys(
        record,
        _LEGACY_OPERATION_KEYS if legacy else _OPERATION_KEYS,
    )
    if legacy:
        require_boolean(record["allow_remote_control_disconnect"])
    return DueOperation(
        operation_id=OperationId(require_string(record["operation_id"])),
        provider_id=ProviderId(require_string(record["provider_id"])),
        account_id=_optional_account_id(record["account_id"]),
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


def _optional_account_id(value: JsonValue) -> SidekickAccountId | None:
    account_id = require_optional_string(value)
    return None if account_id is None else SidekickAccountId(account_id)


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
        "state": operation.state.value,
        "updated_at": canonical_timestamp(operation.updated_at),
    }


def _operation_payload(document: OperationQueueDocument) -> bytes:
    operations: JsonObject = {
        operation_slot(
            operation.provider_id,
            operation.account_id,
            operation.kind,
        ): (_operation_object(operation))
        for operation in document.operations
    }
    return encode_state_object(
        {
            "operations": operations,
            "schema_version": OPERATION_QUEUE_SCHEMA_VERSION,
        },
        MAX_OPERATION_QUEUE_BYTES,
    )

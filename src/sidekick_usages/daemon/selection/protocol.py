"""Strict wire codecs for live selection control payloads."""

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
    TurnId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.models.protocol import EventPayload, RequestPayload
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionProof,
    ParticipantAdoptionRequest,
    ParticipantClientKind,
    ParticipantConnectionRequest,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantNoticeKind,
    ParticipantReadyProof,
    ParticipantReadyRequest,
    ParticipantRegistration,
    SelectionStatus,
    TurnAdmission,
    TurnAdmissionState,
    TurnBeginRequest,
    TurnEndRequest,
)
from sidekick_usages.daemon.types.protocol import EventKind, RequestKind
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.state.fields import (
    require_exact_keys,
    require_integer,
    require_object,
    require_string,
)
from sidekick_usages.persistence.time_codec import (
    canonical_timestamp,
    parse_canonical_timestamp,
)
from sidekick_usages.serialization.json import JsonObject, JsonValue


def encode_selection_request(payload: RequestPayload) -> JsonValue:
    """Encode one selection-owned request payload."""
    if isinstance(payload, ParticipantManifest):
        return {
            "capability_version": payload.capability_version,
            "client_kind": payload.client_kind.value,
            "connection_generation": payload.connection_generation,
            "participant_id": str(payload.participant_id),
            "provider": payload.provider_id.value,
        }
    if isinstance(payload, ParticipantConnectionRequest):
        return _connection_json(payload)
    if isinstance(payload, (TurnBeginRequest, TurnEndRequest)):
        return {
            **_connection_json(payload),
            "turn_id": str(payload.turn_id),
        }
    if isinstance(payload, ParticipantReadyRequest):
        return {
            **_connection_json(payload),
            "account_id": str(payload.proof.account_id),
            "authority_generation": str(payload.proof.generation),
            "epoch": payload.proof.epoch.value,
        }
    if isinstance(payload, ParticipantAdoptionRequest):
        return {
            **_connection_json(payload),
            "account_id": str(payload.proof.account_id),
            "authority_generation": str(payload.proof.generation),
            "epoch": payload.proof.epoch.value,
            "turn_id": str(payload.proof.turn_id),
        }
    raise TypeError("Request payload is not selection-owned.")


def decode_selection_request(
    kind: RequestKind,
    value: JsonValue,
) -> RequestPayload:
    """Decode one selection-owned request payload."""
    try:
        return _decode_selection_request(kind, value)
    except InvalidSchemaError:
        raise ValueError("Selection request payload is malformed.") from None


def _decode_selection_request(
    kind: RequestKind,
    value: JsonValue,
) -> RequestPayload:
    root = require_object(value)
    if kind is RequestKind.PARTICIPANT_REGISTER:
        require_exact_keys(
            root,
            {
                "capability_version",
                "client_kind",
                "connection_generation",
                "participant_id",
                "provider",
            },
        )
        return ParticipantManifest(
            participant_id=_participant(root),
            provider_id=ProviderId(require_string(root["provider"])),
            client_kind=ParticipantClientKind(
                require_string(root["client_kind"])
            ),
            capability_version=require_integer(root["capability_version"]),
            connection_generation=_generation(root),
        )
    if kind is RequestKind.PARTICIPANT_SUBSCRIBE:
        _require_connection_keys(root)
        return ParticipantConnectionRequest(
            _participant(root),
            _generation(root),
        )
    if kind in {RequestKind.TURN_BEGIN, RequestKind.TURN_END}:
        require_exact_keys(
            root,
            {"connection_generation", "participant_id", "turn_id"},
        )
        values = (
            _participant(root),
            _generation(root),
            TurnId(require_string(root["turn_id"])),
        )
        if kind is RequestKind.TURN_BEGIN:
            return TurnBeginRequest(*values)
        return TurnEndRequest(*values)
    if kind is RequestKind.PARTICIPANT_READY:
        require_exact_keys(
            root,
            {
                "account_id",
                "authority_generation",
                "connection_generation",
                "epoch",
                "participant_id",
            },
        )
        return ParticipantReadyRequest(
            participant_id=_participant(root),
            connection_generation=_generation(root),
            proof=ParticipantReadyProof(
                account_id=_account(root["account_id"]),
                generation=_authority(root["authority_generation"]),
                epoch=SelectionEpoch(require_integer(root["epoch"])),
            ),
        )
    if kind is RequestKind.PARTICIPANT_ADOPT:
        require_exact_keys(
            root,
            {
                "account_id",
                "authority_generation",
                "connection_generation",
                "epoch",
                "participant_id",
                "turn_id",
            },
        )
        return ParticipantAdoptionRequest(
            participant_id=_participant(root),
            connection_generation=_generation(root),
            proof=ParticipantAdoptionProof(
                turn_id=TurnId(require_string(root["turn_id"])),
                account_id=_account(root["account_id"]),
                generation=_authority(root["authority_generation"]),
                epoch=SelectionEpoch(require_integer(root["epoch"])),
            ),
        )
    raise TypeError("Request kind is not selection-owned.")


def encode_selection_event(payload: EventPayload) -> JsonValue:
    """Encode one selection-owned event payload."""
    if isinstance(payload, ParticipantRegistration):
        return {
            "connection_generation": payload.connection_generation,
            "participant_id": str(payload.participant_id),
            "pending_epoch": _optional_epoch(payload.pending_epoch),
            "provider": payload.provider_id.value,
            "registered_epoch": payload.registered_epoch.value,
        }
    if isinstance(payload, TurnAdmission):
        return {
            "account_id": _optional_account(payload.account_id),
            "authority_generation": _optional_authority(payload.generation),
            "epoch": _optional_epoch(payload.epoch),
            "participant_id": str(payload.participant_id),
            "state": payload.state.value,
            "turn_id": str(payload.turn_id),
        }
    if isinstance(payload, ParticipantNotice):
        encoded: dict[str, JsonValue] = {
            "code": None if payload.code is None else payload.code.value,
            "epoch": payload.epoch.value,
            "kind": payload.kind.value,
            "participant_id": str(payload.participant_id),
            "provider": payload.provider_id.value,
        }
        if payload.kind is ParticipantNoticeKind.READY:
            encoded.update(
                {
                    "operation_id": str(payload.operation_id),
                    "target_account_id": str(payload.target_account_id),
                    "target_generation": str(payload.target_generation),
                }
            )
        return encoded
    if isinstance(payload, SelectionResult):
        return {
            "adopted_count": payload.adopted_count,
            "completed_at": canonical_timestamp(payload.completed_at),
            "epoch": payload.epoch.value,
            "lost_count": payload.lost_count,
            "operation_id": str(payload.operation_id),
            "outcome": payload.outcome.value,
            "provider": payload.provider_id.value,
            "ready_count": payload.ready_count,
            "required_count": payload.required_count,
            "safe_code": payload.safe_code.value,
            "started_at": canonical_timestamp(payload.started_at),
            "target_account_id": str(payload.target_account_id),
            "target_generation": _optional_authority(
                payload.target_generation
            ),
        }
    if isinstance(payload, SelectionStatus):
        return {
            "active_turn_count": payload.active_turn_count,
            "adopted_count": payload.adopted_count,
            "code": None if payload.code is None else payload.code.value,
            "confirmed_dead_count": payload.confirmed_dead_count,
            "finalized_account_id": _optional_account(
                payload.finalized_account_id
            ),
            "finalized_epoch": _optional_epoch(payload.finalized_epoch),
            "operation_id": _optional_operation(payload.operation_id),
            "pending_epoch": _optional_epoch(payload.pending_epoch),
            "phase": None if payload.phase is None else payload.phase.value,
            "provider": payload.provider_id.value,
            "queued_turn_count": payload.queued_turn_count,
            "reachable_count": payload.reachable_count,
            "ready_count": payload.ready_count,
            "registered_count": payload.registered_count,
            "required_count": payload.required_count,
            "target_account_id": _optional_account(payload.target_account_id),
            "unreachable_count": payload.unreachable_count,
        }
    raise TypeError("Event payload is not selection-owned.")


def decode_selection_event(
    kind: EventKind,
    value: JsonValue,
) -> EventPayload:
    """Decode one selection-owned event payload."""
    try:
        return _decode_selection_event(kind, value)
    except InvalidSchemaError:
        raise ValueError("Selection event payload is malformed.") from None


def _decode_selection_event(
    kind: EventKind,
    value: JsonValue,
) -> EventPayload:
    root = require_object(value)
    if kind is EventKind.PARTICIPANT_REGISTERED:
        require_exact_keys(
            root,
            {
                "connection_generation",
                "participant_id",
                "pending_epoch",
                "provider",
                "registered_epoch",
            },
        )
        return ParticipantRegistration(
            participant_id=_participant(root),
            provider_id=ProviderId(require_string(root["provider"])),
            connection_generation=_generation(root),
            registered_epoch=SelectionEpoch(
                require_integer(root["registered_epoch"])
            ),
            pending_epoch=_decode_optional_epoch(root["pending_epoch"]),
        )
    if kind is EventKind.TURN_ADMISSION:
        require_exact_keys(
            root,
            {
                "account_id",
                "authority_generation",
                "epoch",
                "participant_id",
                "state",
                "turn_id",
            },
        )
        return TurnAdmission(
            participant_id=_participant(root),
            turn_id=TurnId(require_string(root["turn_id"])),
            state=TurnAdmissionState(require_string(root["state"])),
            epoch=_decode_optional_epoch(root["epoch"]),
            account_id=_decode_optional_account(root["account_id"]),
            generation=_decode_optional_authority(
                root["authority_generation"]
            ),
        )
    if kind is EventKind.PARTICIPANT_NOTICE:
        notice_kind = ParticipantNoticeKind(require_string(root.get("kind")))
        keys = {"code", "epoch", "kind", "participant_id", "provider"}
        if notice_kind is ParticipantNoticeKind.READY:
            keys |= {
                "operation_id",
                "target_account_id",
                "target_generation",
            }
        require_exact_keys(
            root,
            keys,
        )
        return ParticipantNotice(
            participant_id=_participant(root),
            provider_id=ProviderId(require_string(root["provider"])),
            kind=notice_kind,
            epoch=SelectionEpoch(require_integer(root["epoch"])),
            code=_decode_optional_code(root["code"]),
            operation_id=(
                OperationId(require_string(root["operation_id"]))
                if notice_kind is ParticipantNoticeKind.READY
                else None
            ),
            target_account_id=(
                _account(root["target_account_id"])
                if notice_kind is ParticipantNoticeKind.READY
                else None
            ),
            target_generation=(
                _authority(root["target_generation"])
                if notice_kind is ParticipantNoticeKind.READY
                else None
            ),
        )
    if kind is EventKind.SELECTION_RESULT:
        return _decode_result(root)
    if kind is EventKind.SELECTION_STATUS:
        return _decode_status(root)
    raise TypeError("Event kind is not selection-owned.")


def _decode_result(root: JsonObject) -> SelectionResult:
    require_exact_keys(
        root,
        {
            "adopted_count",
            "completed_at",
            "epoch",
            "lost_count",
            "operation_id",
            "outcome",
            "provider",
            "ready_count",
            "required_count",
            "safe_code",
            "started_at",
            "target_account_id",
            "target_generation",
        },
    )
    try:
        return SelectionResult(
            operation_id=OperationId(require_string(root["operation_id"])),
            provider_id=ProviderId(require_string(root["provider"])),
            target_account_id=_account(root["target_account_id"]),
            target_generation=_decode_optional_authority(
                root["target_generation"]
            ),
            epoch=SelectionEpoch(require_integer(root["epoch"])),
            outcome=SelectionOutcome(require_string(root["outcome"])),
            safe_code=SelectionCode(require_string(root["safe_code"])),
            required_count=require_integer(root["required_count"]),
            ready_count=require_integer(root["ready_count"]),
            adopted_count=require_integer(root["adopted_count"]),
            lost_count=require_integer(root["lost_count"]),
            started_at=parse_canonical_timestamp(
                require_string(root["started_at"])
            ),
            completed_at=parse_canonical_timestamp(
                require_string(root["completed_at"])
            ),
        )
    except InvalidSchemaError:
        raise ValueError("Selection timestamp is malformed.") from None


def _decode_status(root: JsonObject) -> SelectionStatus:
    require_exact_keys(
        root,
        {
            "active_turn_count",
            "adopted_count",
            "code",
            "confirmed_dead_count",
            "finalized_account_id",
            "finalized_epoch",
            "operation_id",
            "pending_epoch",
            "phase",
            "provider",
            "queued_turn_count",
            "reachable_count",
            "ready_count",
            "registered_count",
            "required_count",
            "target_account_id",
            "unreachable_count",
        },
    )
    return SelectionStatus(
        provider_id=ProviderId(require_string(root["provider"])),
        operation_id=_decode_optional_operation(root["operation_id"]),
        finalized_account_id=_decode_optional_account(
            root["finalized_account_id"]
        ),
        finalized_epoch=_decode_optional_epoch(root["finalized_epoch"]),
        target_account_id=_decode_optional_account(root["target_account_id"]),
        pending_epoch=_decode_optional_epoch(root["pending_epoch"]),
        phase=_decode_optional_phase(root["phase"]),
        code=_decode_optional_code(root["code"]),
        confirmed_dead_count=require_integer(root["confirmed_dead_count"]),
        registered_count=require_integer(root["registered_count"]),
        reachable_count=require_integer(root["reachable_count"]),
        required_count=require_integer(root["required_count"]),
        ready_count=require_integer(root["ready_count"]),
        adopted_count=require_integer(root["adopted_count"]),
        unreachable_count=require_integer(root["unreachable_count"]),
        active_turn_count=require_integer(root["active_turn_count"]),
        queued_turn_count=require_integer(root["queued_turn_count"]),
    )


def _connection_json(
    request: (
        ParticipantConnectionRequest
        | TurnBeginRequest
        | TurnEndRequest
        | ParticipantReadyRequest
        | ParticipantAdoptionRequest
    ),
) -> dict[str, JsonValue]:
    return {
        "connection_generation": request.connection_generation,
        "participant_id": str(request.participant_id),
    }


def _require_connection_keys(root: JsonObject) -> None:
    require_exact_keys(
        root,
        {"connection_generation", "participant_id"},
    )


def _participant(root: JsonObject) -> ParticipantId:
    return ParticipantId(require_string(root["participant_id"]))


def _generation(root: JsonObject) -> int:
    return require_integer(root["connection_generation"])


def _account(value: JsonValue) -> SidekickAccountId:
    return SidekickAccountId(require_string(value))


def _authority(value: JsonValue) -> AuthorityGeneration:
    return AuthorityGeneration(require_string(value))


def _optional_operation(value: OperationId | None) -> JsonValue:
    return None if value is None else str(value)


def _decode_optional_operation(value: JsonValue) -> OperationId | None:
    return None if value is None else OperationId(require_string(value))


def _optional_epoch(value: SelectionEpoch | None) -> JsonValue:
    return None if value is None else value.value


def _decode_optional_epoch(value: JsonValue) -> SelectionEpoch | None:
    return None if value is None else SelectionEpoch(require_integer(value))


def _optional_account(value: SidekickAccountId | None) -> JsonValue:
    return None if value is None else str(value)


def _decode_optional_account(
    value: JsonValue,
) -> SidekickAccountId | None:
    return None if value is None else _account(value)


def _optional_authority(value: AuthorityGeneration | None) -> JsonValue:
    return None if value is None else str(value)


def _decode_optional_authority(
    value: JsonValue,
) -> AuthorityGeneration | None:
    return None if value is None else _authority(value)


def _decode_optional_phase(value: JsonValue) -> SelectionPhase | None:
    if value is None:
        return None
    return SelectionPhase(require_string(value))


def _decode_optional_code(value: JsonValue) -> SelectionCode | None:
    if value is None:
        return None
    return SelectionCode(require_string(value))

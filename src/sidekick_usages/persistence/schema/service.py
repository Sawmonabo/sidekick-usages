"""Strict non-secret codec for resident supervisor state."""

from sidekick_usages.daemon.models.service import (
    ServicePreparationReport,
    ServiceState,
)
from sidekick_usages.daemon.types.service import (
    PackageVersion,
    ServicePhase,
)
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.state.fields import (
    require_boolean,
    require_exact_keys,
    require_integer,
    require_list,
    require_object,
    require_optional_string,
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

SERVICE_STATE_SCHEMA_VERSION = 3
_LEGACY_SERVICE_STATE_SCHEMA_VERSION = 2
MAX_SERVICE_STATE_BYTES = 32 * 1024

_SERVICE_STATE_KEYS = frozenset(
    {
        "active_workers",
        "broker_ready",
        "failure_code",
        "journals_reconciled",
        "observed_at",
        "package_version",
        "phase",
        "preparation_report",
        "protocol_version",
        "queue_recovered",
        "revision",
        "schema_version",
    }
)
_LEGACY_SERVICE_STATE_KEYS = _SERVICE_STATE_KEYS - {"preparation_report"}
_PREPARATION_REPORT_KEYS = frozenset({"dry_run", "operator_steps", "reason"})


def _preparation_object(
    report: ServicePreparationReport | None,
) -> JsonObject | None:
    if report is None:
        return None
    return {
        "dry_run": report.dry_run,
        "operator_steps": list(report.operator_steps),
        "reason": report.reason,
    }


def _decode_preparation_report(
    value: JsonValue,
) -> ServicePreparationReport | None:
    if value is None:
        return None
    report = require_object(value)
    require_exact_keys(report, _PREPARATION_REPORT_KEYS)
    steps = require_list(report["operator_steps"])
    return ServicePreparationReport(
        reason=require_string(report["reason"]),
        operator_steps=tuple(require_string(step) for step in steps),
        dry_run=require_boolean(report["dry_run"]),
    )


def _state_object(
    state: ServiceState,
    *,
    schema_version: int = SERVICE_STATE_SCHEMA_VERSION,
) -> JsonObject:
    root: JsonObject = {
        "active_workers": state.active_workers,
        "broker_ready": state.broker_ready,
        "failure_code": state.failure_code,
        "journals_reconciled": state.journals_reconciled,
        "observed_at": canonical_timestamp(state.observed_at),
        "package_version": str(state.package_version),
        "phase": state.phase.value,
        "protocol_version": state.protocol_version,
        "queue_recovered": state.queue_recovered,
        "revision": state.revision,
        "schema_version": schema_version,
    }
    if schema_version == SERVICE_STATE_SCHEMA_VERSION:
        root["preparation_report"] = _preparation_object(
            state.preparation_report
        )
    return root


def _state_payload(state: ServiceState) -> bytes:
    return encode_state_object(
        _state_object(state),
        MAX_SERVICE_STATE_BYTES,
    )


def decode_service_state(payload: bytes) -> ServiceState:
    """Decode one canonical resident service state."""
    root = decode_state_object(payload, MAX_SERVICE_STATE_BYTES)
    schema_version = require_integer(root.get("schema_version"))
    if schema_version == SERVICE_STATE_SCHEMA_VERSION:
        require_exact_keys(root, _SERVICE_STATE_KEYS)
        preparation_report = _decode_preparation_report(
            root["preparation_report"]
        )
    elif schema_version == _LEGACY_SERVICE_STATE_SCHEMA_VERSION:
        require_exact_keys(root, _LEGACY_SERVICE_STATE_KEYS)
        preparation_report = None
    else:
        raise InvalidSchemaError
    try:
        state = ServiceState(
            protocol_version=require_integer(root["protocol_version"]),
            package_version=PackageVersion(
                require_string(root["package_version"])
            ),
            phase=ServicePhase(require_string(root["phase"])),
            preparation_report=preparation_report,
            revision=require_integer(root["revision"]),
            observed_at=parse_canonical_timestamp(
                require_string(root["observed_at"])
            ),
            queue_recovered=require_boolean(root["queue_recovered"]),
            journals_reconciled=require_boolean(root["journals_reconciled"]),
            broker_ready=require_boolean(root["broker_ready"]),
            active_workers=require_integer(root["active_workers"]),
            failure_code=require_optional_string(root["failure_code"]),
        )
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    canonical = encode_state_object(
        _state_object(state, schema_version=schema_version),
        MAX_SERVICE_STATE_BYTES,
    )
    if canonical != payload:
        raise InvalidSchemaError
    return state


def encode_service_state(state: ServiceState) -> bytes:
    """Encode and prove one canonical resident service state."""
    payload = _state_payload(state)
    if decode_service_state(payload) != state:
        raise InvalidSchemaError
    return payload

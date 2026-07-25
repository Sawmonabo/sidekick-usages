"""Strict non-secret codec for resident supervisor state."""

from sidekick_usages.daemon.models.service import ServiceState
from sidekick_usages.daemon.types.service import (
    PackageVersion,
    ServicePhase,
)
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.state.fields import (
    require_boolean,
    require_exact_keys,
    require_integer,
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
from sidekick_usages.serialization.json import JsonObject

SERVICE_STATE_SCHEMA_VERSION = 2
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
        "protocol_version",
        "queue_recovered",
        "revision",
        "schema_version",
    }
)


def _state_object(state: ServiceState) -> JsonObject:
    return {
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
        "schema_version": SERVICE_STATE_SCHEMA_VERSION,
    }


def _state_payload(state: ServiceState) -> bytes:
    return encode_state_object(
        _state_object(state),
        MAX_SERVICE_STATE_BYTES,
    )


def decode_service_state(payload: bytes) -> ServiceState:
    """Decode one canonical resident service state."""
    root = decode_state_object(payload, MAX_SERVICE_STATE_BYTES)
    require_exact_keys(root, _SERVICE_STATE_KEYS)
    require_schema_version(
        root["schema_version"],
        SERVICE_STATE_SCHEMA_VERSION,
    )
    try:
        state = ServiceState(
            protocol_version=require_integer(root["protocol_version"]),
            package_version=PackageVersion(
                require_string(root["package_version"])
            ),
            phase=ServicePhase(require_string(root["phase"])),
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
    if _state_payload(state) != payload:
        raise InvalidSchemaError
    return state


def encode_service_state(state: ServiceState) -> bytes:
    """Encode and prove one canonical resident service state."""
    payload = _state_payload(state)
    if decode_service_state(payload) != state:
        raise InvalidSchemaError
    return payload

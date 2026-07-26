"""Strict non-secret guided service-setup acknowledgement codec."""

from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.setup.models import (
    ServiceSetupAcknowledgement,
)
from sidekick_usages.persistence.state.fields import (
    require_exact_keys,
    require_integer,
    require_schema_version,
)
from sidekick_usages.persistence.state.json import (
    decode_state_object,
    encode_state_object,
)
from sidekick_usages.serialization.json import JsonObject

SETUP_ACKNOWLEDGEMENT_SCHEMA_VERSION = 1
MAX_SETUP_ACKNOWLEDGEMENT_BYTES = 1024

_SETUP_ACKNOWLEDGEMENT_KEYS = frozenset(
    {
        "protocol_generation",
        "schema_version",
    }
)


def _acknowledgement_object(
    acknowledgement: ServiceSetupAcknowledgement,
) -> JsonObject:
    return {
        "protocol_generation": acknowledgement.protocol_generation,
        "schema_version": SETUP_ACKNOWLEDGEMENT_SCHEMA_VERSION,
    }


def _acknowledgement_payload(
    acknowledgement: ServiceSetupAcknowledgement,
) -> bytes:
    return encode_state_object(
        _acknowledgement_object(acknowledgement),
        MAX_SETUP_ACKNOWLEDGEMENT_BYTES,
    )


def decode_setup_acknowledgement(
    payload: bytes,
) -> ServiceSetupAcknowledgement:
    """Decode one canonical guided service-setup acknowledgement."""
    root = decode_state_object(payload, MAX_SETUP_ACKNOWLEDGEMENT_BYTES)
    require_exact_keys(root, _SETUP_ACKNOWLEDGEMENT_KEYS)
    require_schema_version(
        root["schema_version"],
        SETUP_ACKNOWLEDGEMENT_SCHEMA_VERSION,
    )
    try:
        acknowledgement = ServiceSetupAcknowledgement(
            protocol_generation=require_integer(root["protocol_generation"])
        )
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    if _acknowledgement_payload(acknowledgement) != payload:
        raise InvalidSchemaError
    return acknowledgement


def encode_setup_acknowledgement(
    acknowledgement: ServiceSetupAcknowledgement,
) -> bytes:
    """Encode and prove one canonical setup acknowledgement."""
    payload = _acknowledgement_payload(acknowledgement)
    if decode_setup_acknowledgement(payload) != acknowledgement:
        raise InvalidSchemaError
    return payload

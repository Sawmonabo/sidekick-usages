"""Strict deterministic prototype-import receipt codec."""

from sidekick_usages.persistence import schemas as _schemas
from sidekick_usages.persistence._schema_models import (
    PROTOTYPE_RECEIPT_ADAPTER,
)
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.serialization import JsonObject


def _receipt_object(receipt: _schemas.PrototypeReceipt) -> JsonObject:
    return {
        "receipt_version": 1,
        "prototype_sha256": receipt.prototype_sha256,
        "target_schema_version": receipt.target_schema_version,
    }


def decode_receipt(payload: bytes) -> _schemas.PrototypeReceipt:
    """Decode one exact prototype-import receipt."""
    model = _schemas._validate(
        PROTOTYPE_RECEIPT_ADAPTER,
        _schemas._object_root(payload),
    )
    receipt = _schemas.PrototypeReceipt(
        model.prototype_sha256,
        model.target_schema_version,
    )
    if payload != _schemas._encode_json(_receipt_object(receipt)):
        raise InvalidSchemaError
    return receipt


def encode_receipt(receipt: _schemas.PrototypeReceipt) -> bytes:
    """Encode and strictly re-decode one prototype-import receipt."""
    payload = _schemas._encode_json(_receipt_object(receipt))
    decode_receipt(payload)
    return payload

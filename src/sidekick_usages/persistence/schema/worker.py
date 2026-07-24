"""Strict non-secret codec for isolated worker results."""

from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.daemon.models.worker import WorkerResult
from sidekick_usages.daemon.types.worker import WorkerOutcome
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.state_fields import (
    require_exact_keys,
    require_optional_string,
    require_schema_version,
    require_string,
)
from sidekick_usages.persistence.state_json import (
    decode_state_object,
    encode_state_object,
)
from sidekick_usages.persistence.time_codec import (
    canonical_timestamp,
    parse_canonical_timestamp,
)
from sidekick_usages.serialization import JsonObject

__all__ = [
    "MAX_WORKER_RESULT_BYTES",
    "WORKER_RESULT_SCHEMA_VERSION",
    "decode_worker_result",
    "encode_worker_result",
]

WORKER_RESULT_SCHEMA_VERSION = 1
MAX_WORKER_RESULT_BYTES = 16 * 1024

_WORKER_RESULT_KEYS = frozenset(
    {
        "failure_code",
        "finished_at",
        "operation_id",
        "outcome",
        "schema_version",
    }
)


def _result_object(result: WorkerResult) -> JsonObject:
    return {
        "failure_code": result.failure_code,
        "finished_at": canonical_timestamp(result.finished_at),
        "operation_id": str(result.operation_id),
        "outcome": result.outcome.value,
        "schema_version": WORKER_RESULT_SCHEMA_VERSION,
    }


def _result_payload(result: WorkerResult) -> bytes:
    return encode_state_object(
        _result_object(result),
        MAX_WORKER_RESULT_BYTES,
    )


def decode_worker_result(payload: bytes) -> WorkerResult:
    """Decode one canonical isolated-worker result."""
    root = decode_state_object(payload, MAX_WORKER_RESULT_BYTES)
    require_exact_keys(root, _WORKER_RESULT_KEYS)
    require_schema_version(
        root["schema_version"],
        WORKER_RESULT_SCHEMA_VERSION,
    )
    try:
        result = WorkerResult(
            operation_id=OperationId(require_string(root["operation_id"])),
            outcome=WorkerOutcome(require_string(root["outcome"])),
            finished_at=parse_canonical_timestamp(
                require_string(root["finished_at"])
            ),
            failure_code=require_optional_string(root["failure_code"]),
        )
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    if _result_payload(result) != payload:
        raise InvalidSchemaError
    return result


def encode_worker_result(result: WorkerResult) -> bytes:
    """Encode and prove one canonical isolated-worker result."""
    payload = _result_payload(result)
    if decode_worker_result(payload) != result:
        raise InvalidSchemaError
    return payload

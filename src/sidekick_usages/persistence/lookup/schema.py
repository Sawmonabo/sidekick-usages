"""Strict codec for the latest metrics-refresh observation."""

from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.state.fields import (
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
from sidekick_usages.usage.lookup.models import (
    MetricsRefreshFailureCode,
    MetricsRefreshObservation,
    MetricsRefreshOutcome,
    MetricsRefreshStage,
)
from sidekick_usages.usage.lookup.worker.models import UsageLookupFailure

METRICS_REFRESH_SCHEMA_VERSION = 1
MAX_METRICS_REFRESH_BYTES = 1024

_METRICS_REFRESH_KEYS = frozenset(
    {
        "attempts",
        "code",
        "observed_at",
        "outcome",
        "schema_version",
        "stage",
    }
)


def _observation_object(
    observation: MetricsRefreshObservation,
) -> JsonObject:
    return {
        "attempts": observation.attempts,
        "code": None if observation.code is None else observation.code.value,
        "observed_at": canonical_timestamp(observation.observed_at),
        "outcome": observation.outcome.value,
        "schema_version": METRICS_REFRESH_SCHEMA_VERSION,
        "stage": (
            None if observation.stage is None else observation.stage.value
        ),
    }


def _observation_payload(
    observation: MetricsRefreshObservation,
) -> bytes:
    return encode_state_object(
        _observation_object(observation),
        MAX_METRICS_REFRESH_BYTES,
    )


def _failure_code(
    value: str | None,
) -> UsageLookupFailure | MetricsRefreshFailureCode | None:
    if value is None:
        return None
    try:
        return UsageLookupFailure(value)
    except ValueError:
        return MetricsRefreshFailureCode(value)


def decode_metrics_refresh_observation(
    payload: bytes,
) -> MetricsRefreshObservation:
    """Decode one canonical metrics-refresh observation."""
    root = decode_state_object(payload, MAX_METRICS_REFRESH_BYTES)
    require_exact_keys(root, _METRICS_REFRESH_KEYS)
    require_schema_version(
        root["schema_version"],
        METRICS_REFRESH_SCHEMA_VERSION,
    )
    stage = require_optional_string(root["stage"])
    code = require_optional_string(root["code"])
    try:
        observation = MetricsRefreshObservation(
            observed_at=parse_canonical_timestamp(
                require_string(root["observed_at"])
            ),
            outcome=MetricsRefreshOutcome(require_string(root["outcome"])),
            attempts=require_integer(root["attempts"]),
            stage=None if stage is None else MetricsRefreshStage(stage),
            code=_failure_code(code),
        )
    except TypeError, ValueError:
        raise InvalidSchemaError from None
    if _observation_payload(observation) != payload:
        raise InvalidSchemaError
    return observation


def encode_metrics_refresh_observation(
    observation: MetricsRefreshObservation,
) -> bytes:
    """Encode and prove one canonical metrics-refresh observation."""
    payload = _observation_payload(observation)
    if decode_metrics_refresh_observation(payload) != observation:
        raise InvalidSchemaError
    return payload

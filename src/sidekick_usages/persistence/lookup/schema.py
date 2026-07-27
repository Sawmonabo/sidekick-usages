"""Strict codec for the latest metrics-refresh observation."""

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import InvalidSchemaError
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
from sidekick_usages.persistence.types.error import PersistenceCode
from sidekick_usages.serialization.json import JsonObject, JsonValue
from sidekick_usages.usage.lookup.diagnostics.models import (
    MetricsRefreshCause,
    MetricsRefreshFailureCode,
    MetricsRefreshObservation,
    MetricsRefreshOutcome,
    MetricsRefreshStage,
)
from sidekick_usages.usage.lookup.worker.models import UsageLookupFailure
from sidekick_usages.usage.models import FetchFailureKind

METRICS_REFRESH_SCHEMA_VERSION = 2
MAX_METRICS_REFRESH_BYTES = 256 * 1024

_METRICS_REFRESH_CAUSE_KEYS = frozenset(
    {
        "account_id",
        "code",
        "provider_id",
        "stage",
    }
)
_METRICS_REFRESH_KEYS = frozenset(
    {
        "attempts",
        "causes",
        "observed_at",
        "outcome",
        "retry_causes",
        "schema_version",
    }
)


def _cause_object(cause: MetricsRefreshCause) -> JsonObject:
    return {
        "account_id": (
            None if cause.account_id is None else str(cause.account_id)
        ),
        "code": cause.code.value,
        "provider_id": (
            None if cause.provider_id is None else cause.provider_id.value
        ),
        "stage": cause.stage.value,
    }


def _cause_objects(
    causes: tuple[MetricsRefreshCause, ...],
) -> list[JsonValue]:
    return [_cause_object(cause) for cause in causes]


def _observation_object(
    observation: MetricsRefreshObservation,
) -> JsonObject:
    return {
        "attempts": observation.attempts,
        "causes": _cause_objects(observation.causes),
        "observed_at": canonical_timestamp(observation.observed_at),
        "outcome": observation.outcome.value,
        "retry_causes": _cause_objects(observation.retry_causes),
        "schema_version": METRICS_REFRESH_SCHEMA_VERSION,
    }


def _observation_payload(
    observation: MetricsRefreshObservation,
) -> bytes:
    return encode_state_object(
        _observation_object(observation),
        MAX_METRICS_REFRESH_BYTES,
    )


def _failure_code(
    stage: MetricsRefreshStage,
    value: str,
) -> (
    UsageLookupFailure
    | FetchFailureKind
    | PersistenceCode
    | MetricsRefreshFailureCode
):
    if stage is MetricsRefreshStage.WORKER:
        return UsageLookupFailure(value)
    if stage is MetricsRefreshStage.ACCOUNT:
        return FetchFailureKind(value)
    if stage is MetricsRefreshStage.SNAPSHOT_RELOAD:
        if value == MetricsRefreshFailureCode.SNAPSHOT_UNAVAILABLE.value:
            return MetricsRefreshFailureCode.SNAPSHOT_UNAVAILABLE
        return PersistenceCode(value)
    return MetricsRefreshFailureCode(value)


def _decode_cause(value: JsonValue) -> MetricsRefreshCause:
    root = require_object(value)
    require_exact_keys(root, _METRICS_REFRESH_CAUSE_KEYS)
    stage = MetricsRefreshStage(require_string(root["stage"]))
    provider_id = require_optional_string(root["provider_id"])
    account_id = require_optional_string(root["account_id"])
    return MetricsRefreshCause(
        stage=stage,
        code=_failure_code(stage, require_string(root["code"])),
        provider_id=(None if provider_id is None else ProviderId(provider_id)),
        account_id=(
            None if account_id is None else SidekickAccountId(account_id)
        ),
    )


def _decode_causes(value: JsonValue) -> tuple[MetricsRefreshCause, ...]:
    return tuple(_decode_cause(item) for item in require_list(value))


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
    try:
        observation = MetricsRefreshObservation(
            observed_at=parse_canonical_timestamp(
                require_string(root["observed_at"])
            ),
            outcome=MetricsRefreshOutcome(require_string(root["outcome"])),
            attempts=require_integer(root["attempts"]),
            retry_causes=_decode_causes(root["retry_causes"]),
            causes=_decode_causes(root["causes"]),
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

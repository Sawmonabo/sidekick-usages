"""Strict atomic credential stage codec."""

import base64
import binascii
import json
from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    TypeAdapter,
    ValidationError,
)

from sidekick_usages.core.models import CodexCredentials, Credentials
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.persistence.models.refresh import (
    DecodedCredentialRefreshStage,
)
from sidekick_usages.persistence.private.bundles.writes import (
    MAX_PRIVATE_BUNDLE_BYTES,
    MAX_PRIVATE_FILE_BYTES,
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.schema.config import STRICT_SCHEMA_CONFIG
from sidekick_usages.persistence.schema.credential import (
    CredentialDecodeError,
    decode_credentials,
    encode_credentials,
)
from sidekick_usages.persistence.schema.refresh.stage.private import (
    decode_private_refresh_stage,
    encode_private_refresh_stage,
)
from sidekick_usages.persistence.time_codec import (
    canonical_timestamp,
    canonical_timestamp_text,
    parse_canonical_timestamp,
)
from sidekick_usages.serialization.json import (
    JsonDecodeError,
    decode_json_value,
)

SCHEMA_VERSION = 1


type CredentialBase64 = Annotated[
    str,
    AfterValidator(_credential_base64),
]
type BundleBase64 = Annotated[str, AfterValidator(_bundle_base64)]
type LabelValue = Annotated[str, AfterValidator(_label)]
type TimestampValue = Annotated[
    str,
    AfterValidator(canonical_timestamp_text),
]


class CredentialRefreshStageDecodeError(ValueError):
    """A combined refresh stage violates its strict contract."""


def _bounded_base64(value: str, maximum: int) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except binascii.Error, ValueError:
        raise ValueError from None
    if len(decoded) > maximum:
        raise ValueError
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError
    return value


def _credential_base64(value: str) -> str:
    validated = _bounded_base64(value, MAX_PRIVATE_FILE_BYTES)
    try:
        decode_credentials(base64.b64decode(validated, validate=True))
    except CredentialDecodeError, binascii.Error:
        raise ValueError from None
    return validated


def _bundle_base64(value: str) -> str:
    return _bounded_base64(value, MAX_PRIVATE_BUNDLE_BYTES)


def _label(value: str) -> str:
    AccountLabel(value)
    return value


class _CredentialRefreshStage(BaseModel):
    """One atomically published secret replacement envelope."""

    model_config = STRICT_SCHEMA_CONFIG

    schema_version: Literal[1]
    account_label: LabelValue
    credential_base64: CredentialBase64
    completed_at: TimestampValue
    plan_update: str | None
    private_bundle_base64: BundleBase64 | None


def _encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _credential_home(credentials: Credentials) -> str | None:
    return (
        credentials.auth_home
        if isinstance(credentials, CodexCredentials)
        else None
    )


def _require_bundle_matches(
    credentials: Credentials,
    private_bundle: PreparedPrivateBundleWrite | None,
) -> None:
    home = _credential_home(credentials)
    if (home is None) is not (private_bundle is None) or (
        private_bundle is not None and str(private_bundle.path) != home
    ):
        raise CredentialRefreshStageDecodeError


def encode_credential_refresh_stage(
    label: AccountLabel,
    credentials: Credentials,
    completed_at: datetime,
    plan_update: str | None,
    private_bundle: PreparedPrivateBundleWrite | None,
) -> bytes:
    """Encode one credential result and optional bundle atomically."""
    _require_bundle_matches(credentials, private_bundle)
    private_payload = (
        None
        if private_bundle is None
        else encode_private_refresh_stage(private_bundle)
    )
    stage = _CredentialRefreshStage(
        schema_version=SCHEMA_VERSION,
        account_label=str(label),
        credential_base64=_encoded(encode_credentials(credentials)),
        completed_at=canonical_timestamp(completed_at),
        plan_update=plan_update,
        private_bundle_base64=(
            None if private_payload is None else _encoded(private_payload)
        ),
    )
    payload = json.dumps(
        stage.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    decode_credential_refresh_stage(payload)
    return payload


def decode_credential_refresh_stage(
    payload: bytes,
) -> DecodedCredentialRefreshStage:
    """Decode one complete credential and optional bundle replacement."""
    try:
        value = decode_json_value(payload)
        stage = TypeAdapter(_CredentialRefreshStage).validate_python(
            value,
            strict=True,
        )
        credentials = decode_credentials(
            base64.b64decode(stage.credential_base64, validate=True)
        )
        private_bundle = (
            None
            if stage.private_bundle_base64 is None
            else decode_private_refresh_stage(
                base64.b64decode(
                    stage.private_bundle_base64,
                    validate=True,
                )
            )
        )
        _require_bundle_matches(credentials, private_bundle)
        return DecodedCredentialRefreshStage(
            AccountLabel(stage.account_label),
            credentials,
            parse_canonical_timestamp(stage.completed_at),
            stage.plan_update,
            private_bundle,
        )
    except (
        CredentialDecodeError,
        JsonDecodeError,
        ValidationError,
        binascii.Error,
        TypeError,
        ValueError,
    ):
        raise CredentialRefreshStageDecodeError from None

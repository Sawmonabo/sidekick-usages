"""Strict codec for one atomic credential-refresh secret stage."""

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    TypeAdapter,
    ValidationError,
)

from sidekick_usages.core.models import Account
from sidekick_usages.persistence.credential_refresh_private_stage import (
    decode_private_refresh_stage,
    encode_private_refresh_stage,
)
from sidekick_usages.persistence.private_bundle_writes import (
    MAX_PRIVATE_BUNDLE_BYTES,
    MAX_PRIVATE_FILE_BYTES,
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.schema.refresh import (
    decode_staged_account,
)
from sidekick_usages.persistence.schemas import encode_version_two
from sidekick_usages.persistence.transforms import accounts_to_version_two
from sidekick_usages.serialization import JsonDecodeError, decode_json_value

_SCHEMA_VERSION = 1


class CredentialRefreshStageDecodeError(ValueError):
    """A combined refresh stage violates its strict contract."""


@dataclass(frozen=True, slots=True)
class DecodedCredentialRefreshStage:
    """One validated account replacement and optional companion bundle."""

    account: Account = field(repr=False)
    plan_update: str | None
    private_bundle: PreparedPrivateBundleWrite | None = field(repr=False)


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


def _account_base64(value: str) -> str:
    return _bounded_base64(value, MAX_PRIVATE_FILE_BYTES)


def _bundle_base64(value: str) -> str:
    return _bounded_base64(value, MAX_PRIVATE_BUNDLE_BYTES)


type _AccountBase64 = Annotated[str, AfterValidator(_account_base64)]
type _BundleBase64 = Annotated[str, AfterValidator(_bundle_base64)]


class _CredentialRefreshStage(BaseModel):
    """One atomically published secret replacement envelope."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[1]
    account_base64: _AccountBase64
    plan_update: str | None
    private_bundle_base64: _BundleBase64 | None


_STAGE_ADAPTER = TypeAdapter(_CredentialRefreshStage)


def _encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def encode_credential_refresh_stage(
    account: Account,
    plan_update: str | None,
    private_bundle: PreparedPrivateBundleWrite | None,
) -> bytes:
    """Encode account and optional bundle as one atomic secret envelope."""
    account_payload = encode_version_two(accounts_to_version_two((account,)))
    private_payload = (
        None
        if private_bundle is None
        else encode_private_refresh_stage(private_bundle)
    )
    stage = _CredentialRefreshStage(
        schema_version=_SCHEMA_VERSION,
        account_base64=_encoded(account_payload),
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
    """Decode one complete account and optional bundle replacement."""
    try:
        value = decode_json_value(payload)
        stage = _STAGE_ADAPTER.validate_python(value, strict=True)
        account = decode_staged_account(
            base64.b64decode(stage.account_base64, validate=True)
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
        if stage.plan_update is not None and account.plan != stage.plan_update:
            raise CredentialRefreshStageDecodeError
        if (account.codex_home is None) is not (private_bundle is None) or (
            private_bundle is not None
            and str(private_bundle.path) != account.codex_home
        ):
            raise CredentialRefreshStageDecodeError
        return DecodedCredentialRefreshStage(
            account,
            stage.plan_update,
            private_bundle,
        )
    except (
        JsonDecodeError,
        ValidationError,
        binascii.Error,
        TypeError,
        ValueError,
    ):
        raise CredentialRefreshStageDecodeError from None


__all__ = [
    "CredentialRefreshStageDecodeError",
    "DecodedCredentialRefreshStage",
    "decode_credential_refresh_stage",
    "encode_credential_refresh_stage",
]

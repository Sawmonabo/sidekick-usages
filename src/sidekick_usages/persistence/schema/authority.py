"""Strict codec for protected credential authorities."""

import base64
import binascii
import json
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ValidationError,
)

from sidekick_usages.core.accounts.types import (
    AuthorityId,
    SidekickAccountId,
)
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.limits import MAX_DOCUMENT_BYTES
from sidekick_usages.persistence.models.credential import (
    StoredCredentialAuthority,
    stored_credential_kind,
)
from sidekick_usages.persistence.schema.config import STRICT_SCHEMA_CONFIG
from sidekick_usages.persistence.schema.credential import (
    CredentialDecodeError,
    decode_credentials,
    encode_credentials,
)
from sidekick_usages.persistence.schema.validation import (
    canonical_account_id_text,
)
from sidekick_usages.serialization.json import (
    JsonDecodeError,
    decode_json_value,
)

AUTHORITY_BASENAME = "authority.json"
AUTHORITY_SCHEMA_VERSION = 1


type UuidValue = Annotated[
    str,
    AfterValidator(canonical_account_id_text),
]
type CredentialBase64 = Annotated[
    str,
    AfterValidator(_credential_base64),
]


def _credential_base64(value: str) -> str:
    try:
        payload = base64.b64decode(value, validate=True)
        decode_credentials(payload)
    except binascii.Error, CredentialDecodeError, ValueError:
        raise ValueError from None
    if base64.b64encode(payload).decode("ascii") != value:
        raise ValueError
    return value


class _AuthorityModel(BaseModel):
    """Strict protected credential authority envelope."""

    model_config = STRICT_SCHEMA_CONFIG

    schema_version: Literal[1]
    authority_id: UuidValue
    account_id: UuidValue
    credential_base64: CredentialBase64


def encode_credential_authority(
    authority: StoredCredentialAuthority,
) -> bytes:
    """Encode one strict protected credential authority."""
    try:
        value = {
            "schema_version": AUTHORITY_SCHEMA_VERSION,
            "authority_id": str(authority.authority_id),
            "account_id": str(authority.account_id),
            "credential_base64": base64.b64encode(
                encode_credentials(authority.credentials)
            ).decode("ascii"),
        }
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (
        CredentialDecodeError,
        TypeError,
        UnicodeEncodeError,
        ValueError,
    ):
        raise InvalidSchemaError from None
    if (
        len(payload) > MAX_DOCUMENT_BYTES
        or decode_credential_authority(payload) != authority
    ):
        raise InvalidSchemaError
    return payload


def decode_credential_authority(
    payload: bytes,
) -> StoredCredentialAuthority:
    """Decode one strict protected credential authority."""
    if len(payload) > MAX_DOCUMENT_BYTES:
        raise InvalidSchemaError
    try:
        value = decode_json_value(payload)
        model = _AuthorityModel.model_validate(value, strict=True)
        credentials = decode_credentials(
            base64.b64decode(model.credential_base64, validate=True)
        )
        return StoredCredentialAuthority(
            authority_id=AuthorityId(model.authority_id),
            account_id=SidekickAccountId(model.account_id),
            provider_id=credentials.provider_id,
            kind=stored_credential_kind(credentials),
            credentials=credentials,
        )
    except (
        binascii.Error,
        CredentialDecodeError,
        JsonDecodeError,
        ValidationError,
        TypeError,
        ValueError,
    ):
        raise InvalidSchemaError from None

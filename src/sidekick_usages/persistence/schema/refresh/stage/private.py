"""Strict private credential-bundle stage codec."""

import base64
import binascii
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    ValidationError,
    model_validator,
)

from sidekick_usages.persistence.artifacts import (
    require_portable_unique_basenames,
    require_safe_basename,
)
from sidekick_usages.persistence.private.bundles.writes import (
    MAX_PRIVATE_FILE_BYTES,
    PreparedPrivateBundleWrite,
)
from sidekick_usages.serialization.json import (
    JsonDecodeError,
    decode_json_value,
)

_SCHEMA_VERSION = 1
_MAX_PATH_BYTES = 4096

type _Basename = Annotated[str, AfterValidator(_basename_value)]
type _PathValue = Annotated[str, AfterValidator(_path_value)]
type _Base64Value = Annotated[str, AfterValidator(_base64_value)]
type _ExpectedState = Literal["unchecked", "absent", "present"]


class RefreshPrivateStageDecodeError(ValueError):
    """A private refresh bundle stage violates its strict contract."""


def _basename_value(value: str) -> str:
    require_safe_basename(value)
    return value


def _path_value(value: str) -> str:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError from None
    if not encoded or len(encoded) > _MAX_PATH_BYTES or "\0" in value:
        raise ValueError
    return value


def _base64_value(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except binascii.Error, ValueError:
        raise ValueError from None
    if len(decoded) > MAX_PRIVATE_FILE_BYTES:
        raise ValueError
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ValueError
    return value


class _PrivateFileStage(BaseModel):
    """One bounded target and exact optional base expectation."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    target_base64: _Base64Value
    expected_state: _ExpectedState
    expected_base64: _Base64Value | None

    @model_validator(mode="after")
    def _require_expected_state(self) -> _PrivateFileStage:
        if (self.expected_state == "present") is (
            self.expected_base64 is None
        ):
            raise ValueError
        return self


class _PrivateBundleStage(BaseModel):
    """One complete prepared private-bundle write."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[1]
    path: _PathValue
    expected_bundle_present: bool
    files: dict[_Basename, _PrivateFileStage]

    @model_validator(mode="after")
    def _require_files(self) -> _PrivateBundleStage:
        require_portable_unique_basenames(self.files)
        if not self.files:
            raise ValueError
        return self


def _encoded(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def encode_private_refresh_stage(
    bundle: PreparedPrivateBundleWrite,
) -> bytes:
    """Encode and strictly re-decode one validated prepared bundle."""
    files: dict[str, _PrivateFileStage] = {}
    for basename, target in sorted(bundle.files.items()):
        if basename not in bundle.expected_files:
            state: _ExpectedState = "unchecked"
            expected = None
        elif bundle.expected_files[basename] is None:
            state = "absent"
            expected = None
        else:
            state = "present"
            source = bundle.expected_files[basename]
            if source is None:
                raise AssertionError("Present private source is absent.")
            expected = _encoded(source)
        files[basename] = _PrivateFileStage(
            target_base64=_encoded(target),
            expected_state=state,
            expected_base64=expected,
        )
    stage = _PrivateBundleStage(
        schema_version=_SCHEMA_VERSION,
        path=str(bundle.path),
        expected_bundle_present=bundle.expected_bundle_present,
        files=files,
    )
    payload = json.dumps(
        stage.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    decode_private_refresh_stage(payload)
    return payload


def decode_private_refresh_stage(
    payload: bytes,
) -> PreparedPrivateBundleWrite:
    """Decode one complete immutable prepared private-bundle write."""
    try:
        value = decode_json_value(payload)
        stage = _PrivateBundleStage.model_validate(value, strict=True)
        files = {
            basename: base64.b64decode(file.target_base64, validate=True)
            for basename, file in stage.files.items()
        }
        expected: dict[str, bytes | None] = {}
        for basename, file in stage.files.items():
            if file.expected_state == "unchecked":
                continue
            if file.expected_state == "absent":
                expected[basename] = None
                continue
            source = file.expected_base64
            if source is None:
                raise RefreshPrivateStageDecodeError
            expected[basename] = base64.b64decode(source, validate=True)
        return PreparedPrivateBundleWrite(
            path=Path(stage.path),
            files=files,
            expected_bundle_present=stage.expected_bundle_present,
            expected_files=expected,
        )
    except (
        JsonDecodeError,
        ValidationError,
        binascii.Error,
        TypeError,
        ValueError,
    ):
        raise RefreshPrivateStageDecodeError from None

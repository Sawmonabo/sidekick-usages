"""Strict non-secret schema for private credential transaction recovery."""

import json
import re
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    ExpectedAuthority,
    Sha256Digest,
    portable_basename_key,
    require_portable_unique_basenames,
    require_safe_basename,
)
from sidekick_usages.persistence.errors import InterruptedArtifactError
from sidekick_usages.persistence.private_credentials import (
    PRIVATE_TRANSACTION_DIRECTORY,
    PRIVATE_TRANSACTION_JOURNAL,
)
from sidekick_usages.persistence.schemas import (
    MAX_ACCOUNTS,
    MAX_DOCUMENT_BYTES,
)
from sidekick_usages.serialization import JsonDecodeError, decode_json_value

_JOURNAL_VERSION = 1
_MAX_JOURNAL_BYTES = 4 * 1024 * 1024
_MAX_PRIVATE_FILES_PER_BUNDLE = 16
_MAX_TRANSACTION_FILES = MAX_ACCOUNTS * _MAX_PRIVATE_FILES_PER_BUNDLE
_MAX_BASENAME_BYTES = 255
_STAGE_PATTERN = re.compile(r"stage-[0-9]{4}\.bin\Z", re.ASCII)
_BACKUP_PATTERN = re.compile(r"backup-[0-9]{4}\.bin\Z", re.ASCII)


def _safe_basename(value: str) -> str:
    require_safe_basename(value)
    if len(value.encode("utf-8")) > _MAX_BASENAME_BYTES:
        raise ValueError
    return value


def _digest(value: str) -> str:
    Sha256Digest(value)
    return value


def _stage_basename(value: str) -> str:
    if _STAGE_PATTERN.fullmatch(value) is None:
        raise ValueError
    return value


def _backup_basename(value: str) -> str:
    if _BACKUP_PATTERN.fullmatch(value) is None:
        raise ValueError
    return value


type _SafeBasename = Annotated[str, AfterValidator(_safe_basename)]
type _Digest = Annotated[str, AfterValidator(_digest)]
type _StageBasename = Annotated[str, AfterValidator(_stage_basename)]
type _BackupBasename = Annotated[str, AfterValidator(_backup_basename)]


class AbsentAuthority(BaseModel):
    """Journal authority expectation for first persistence."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["absent"]


class PresentAuthority(BaseModel):
    """Exact journal authority fingerprint for an existing store."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    kind: Literal["present"]
    device: int = Field(ge=0)
    inode: int = Field(ge=0)
    size: int = Field(ge=0, le=MAX_DOCUMENT_BYTES)
    sha256: _Digest


type JournalAuthority = Annotated[
    AbsentAuthority | PresentAuthority,
    Field(discriminator="kind"),
]


class CredentialSourceGuardRecord(BaseModel):
    """Non-secret identity and expectation for a retained source authority."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    path_sha256: _Digest
    authority: JournalAuthority


class CredentialTransactionFile(BaseModel):
    """One bounded target-file transition without credential bytes."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    bundle_basename: _SafeBasename
    basename: _SafeBasename
    stage_basename: _StageBasename
    backup_basename: _BackupBasename | None
    base_sha256: _Digest | None
    target_sha256: _Digest

    @model_validator(mode="after")
    def _coherent_backup(self) -> Self:
        if (self.backup_basename is None) is not (self.base_sha256 is None):
            raise ValueError
        return self


class CredentialTransactionJournal(BaseModel):
    """Strict recovery authority containing no credential bytes."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    journal_version: Literal[1]
    base_authority: JournalAuthority
    source_guard: CredentialSourceGuardRecord | None
    target_authority_sha256: _Digest
    target_authority_size: int = Field(ge=0, le=MAX_DOCUMENT_BYTES)
    target_bundles: tuple[_SafeBasename, ...] = Field(max_length=MAX_ACCOUNTS)
    base_present_bundles: tuple[_SafeBasename, ...] = Field(
        max_length=MAX_ACCOUNTS
    )
    files: tuple[CredentialTransactionFile, ...] = Field(
        max_length=_MAX_TRANSACTION_FILES
    )
    displaced_bundles: tuple[_SafeBasename, ...] = Field(
        max_length=MAX_ACCOUNTS
    )

    @model_validator(mode="after")
    def _coherent_names(self) -> Self:
        if bool(self.target_bundles) is not bool(self.files):
            raise ValueError
        target_keys = tuple(
            portable_basename_key(value) for value in self.target_bundles
        )
        base_keys = tuple(
            portable_basename_key(value) for value in self.base_present_bundles
        )
        displaced_keys = tuple(
            portable_basename_key(value) for value in self.displaced_bundles
        )
        require_portable_unique_basenames(self.target_bundles)
        require_portable_unique_basenames(self.base_present_bundles)
        require_portable_unique_basenames(self.displaced_bundles)
        transaction_key = portable_basename_key(PRIVATE_TRANSACTION_DIRECTORY)
        if transaction_key in target_keys or not set(base_keys) <= set(
            target_keys
        ):
            raise ValueError
        file_names = tuple(
            (
                portable_basename_key(item.bundle_basename),
                portable_basename_key(item.basename),
            )
            for item in self.files
        )
        stages = tuple(
            portable_basename_key(item.stage_basename) for item in self.files
        )
        backups = tuple(
            portable_basename_key(item.backup_basename)
            for item in self.files
            if item.backup_basename is not None
        )
        if any(
            len(values) != len(set(values))
            for values in (file_names, stages, backups)
        ):
            raise ValueError
        if (
            transaction_key in displaced_keys
            or set(target_keys) & set(displaced_keys)
            or set(target_keys)
            != {
                portable_basename_key(item.bundle_basename)
                for item in self.files
            }
        ):
            raise ValueError
        return self


_JOURNAL_ADAPTER = TypeAdapter(CredentialTransactionJournal)


def journal_authority(expected: ExpectedAuthority) -> JournalAuthority:
    """Encode one exact old-authority expectation for recovery."""
    if expected is AuthorityExpectation.ABSENT:
        return AbsentAuthority(kind="absent")
    return PresentAuthority(
        kind="present",
        device=expected.identity.device,
        inode=expected.identity.inode,
        size=expected.size,
        sha256=str(expected.digest),
    )


def encode_credential_journal(
    journal: CredentialTransactionJournal,
) -> bytes:
    """Return bounded deterministic non-secret journal bytes."""
    payload = json.dumps(
        journal.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    if len(payload) > _MAX_JOURNAL_BYTES:
        raise ValueError(
            "Private credential transaction journal is too large."
        )
    return payload


def decode_credential_journal(
    payload: bytes,
) -> CredentialTransactionJournal:
    """Decode a bounded strict journal or fail closed without input detail."""
    if len(payload) > _MAX_JOURNAL_BYTES:
        raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL)
    try:
        value = decode_json_value(payload)
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return _JOURNAL_ADAPTER.validate_json(canonical, strict=True)
    except JsonDecodeError, ValidationError:
        raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL) from None


__all__ = [
    "AbsentAuthority",
    "CredentialSourceGuardRecord",
    "CredentialTransactionFile",
    "CredentialTransactionJournal",
    "PresentAuthority",
    "decode_credential_journal",
    "encode_credential_journal",
    "journal_authority",
]

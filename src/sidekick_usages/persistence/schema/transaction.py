"""Strict non-secret schema for private credential transaction recovery."""

import json
import re
from functools import cache
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
    AuthorityGeneration,
    ExpectedAuthority,
    Sha256Digest,
    portable_basename_key,
    require_portable_unique_basenames,
    require_safe_basename,
)
from sidekick_usages.persistence.errors import InterruptedArtifactError
from sidekick_usages.persistence.limits import (
    MAX_ACCOUNTS,
    MAX_DOCUMENT_BYTES,
)
from sidekick_usages.persistence.private_bundle_paths import (
    PRIVATE_TRANSACTION_DIRECTORY,
    PRIVATE_TRANSACTION_JOURNAL,
    portable_private_bundle_path_key,
    private_bundle_relative_components,
    require_portable_unique_private_bundle_paths,
)
from sidekick_usages.serialization import JsonDecodeError, decode_json_value

_MAX_JOURNAL_BYTES = 4 * 1024 * 1024
_MAX_PRIVATE_FILES_PER_BUNDLE = 16
_MAX_TRANSACTION_FILES = MAX_ACCOUNTS * _MAX_PRIVATE_FILES_PER_BUNDLE
_MAX_BASENAME_BYTES = 255
_STAGE_PATTERN = re.compile(r"stage-[0-9]{4}\.bin\Z", re.ASCII)
_BACKUP_PATTERN = re.compile(r"backup-[0-9]{4}\.bin\Z", re.ASCII)

type _SafeBasename = Annotated[str, AfterValidator(_safe_basename)]
type _Digest = Annotated[str, AfterValidator(_digest)]
type _StageBasename = Annotated[str, AfterValidator(_stage_basename)]
type _BackupBasename = Annotated[str, AfterValidator(_backup_basename)]
type _RelativeBundlePath = Annotated[
    str,
    AfterValidator(_relative_bundle_path),
]
type JournalAuthority = Annotated[
    AbsentAuthority | PresentAuthority,
    Field(discriminator="kind"),
]
type CredentialJournal = Annotated[
    CredentialTransactionJournal | MigrationCredentialTransactionJournal,
    Field(discriminator="journal_version"),
]


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


def _relative_bundle_path(value: str) -> str:
    private_bundle_relative_components(value)
    return value


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


class MigrationCredentialTransactionFile(BaseModel):
    """One migration-only nested target-file transition."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    bundle_path: _RelativeBundlePath
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


class MigrationCredentialTransactionJournal(BaseModel):
    """Strict migration journal with explicit generations and nested paths."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    journal_version: Literal[2]
    base_authority: JournalAuthority
    base_generation: AuthorityGeneration | None
    source_guard: CredentialSourceGuardRecord
    target_generation: AuthorityGeneration
    target_authority_sha256: _Digest
    target_authority_size: int = Field(ge=0, le=MAX_DOCUMENT_BYTES)
    target_bundles: tuple[_RelativeBundlePath, ...] = Field(
        max_length=MAX_ACCOUNTS
    )
    base_present_bundles: tuple[_RelativeBundlePath, ...] = Field(
        max_length=MAX_ACCOUNTS
    )
    files: tuple[MigrationCredentialTransactionFile, ...] = Field(
        max_length=_MAX_TRANSACTION_FILES
    )
    displaced_bundles: tuple[_RelativeBundlePath, ...] = Field(
        max_length=MAX_ACCOUNTS
    )

    @model_validator(mode="after")
    def _coherent_migration(self) -> Self:
        if isinstance(self.base_authority, AbsentAuthority):
            if self.base_generation is not None:
                raise ValueError
        elif self.base_generation is None:
            raise ValueError
        if bool(self.target_bundles) is not bool(self.files):
            raise ValueError
        require_portable_unique_private_bundle_paths(self.target_bundles)
        require_portable_unique_private_bundle_paths(self.base_present_bundles)
        require_portable_unique_private_bundle_paths(self.displaced_bundles)
        target_keys = tuple(
            portable_private_bundle_path_key(value)
            for value in self.target_bundles
        )
        base_keys = tuple(
            portable_private_bundle_path_key(value)
            for value in self.base_present_bundles
        )
        displaced_keys = tuple(
            portable_private_bundle_path_key(value)
            for value in self.displaced_bundles
        )
        file_names = tuple(
            (
                portable_private_bundle_path_key(item.bundle_path),
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
            not set(base_keys) <= set(target_keys)
            or set(target_keys) & set(displaced_keys)
            or set(target_keys)
            != {
                portable_private_bundle_path_key(item.bundle_path)
                for item in self.files
            }
        ):
            raise ValueError
        return self


@cache
def _journal_adapter() -> TypeAdapter[CredentialJournal]:
    """Build the strict journal adapter once without a late global."""
    return TypeAdapter(CredentialJournal)


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
    journal: CredentialJournal,
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
) -> CredentialJournal:
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
        return _journal_adapter().validate_json(canonical, strict=True)
    except JsonDecodeError, ValidationError:
        raise InterruptedArtifactError(PRIVATE_TRANSACTION_JOURNAL) from None

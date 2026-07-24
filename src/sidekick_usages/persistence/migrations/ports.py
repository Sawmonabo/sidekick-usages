"""Provider-neutral ports for private-auth location migration."""

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from sidekick_usages.core.models import Account
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.persistence.artifacts import Sha256Digest
from sidekick_usages.persistence.limits import MAX_ACCOUNTS
from sidekick_usages.persistence.private_credentials import (
    PreparedPrivateBundleWrite,
    private_bundle_relative_components,
    require_portable_unique_private_bundle_paths,
)

__all__ = [
    "PreparedPrivateAuthCopy",
    "PreparedPrivateAuthMigration",
    "PreparedPrivateBundleWrite",
    "PrivateAuthAccountAssessment",
    "PrivateAuthBundleSnapshot",
    "PrivateAuthHomeKind",
    "PrivateAuthMigrationAssessment",
    "PrivateAuthMigrationFailure",
    "PrivateAuthMigrationFailureCode",
    "PrivateAuthMigrationRequest",
    "PrivateAuthMigrationResult",
    "PrivateAuthMigrator",
    "PrivateAuthPermission",
]

_MAX_PRIVATE_AUTH_FAILURE_MESSAGE_BYTES = 1024


class PrivateAuthHomeKind(StrEnum):
    """Closed ownership classes for one persisted private-auth home."""

    UNSET = "unset"
    COMPATIBILITY = "compatibility"
    CANONICAL = "canonical"
    PROVIDER_NATIVE = "provider_native"
    EXTERNAL = "external"


class PrivateAuthPermission(StrEnum):
    """Permission contract for prepared provider credential copies."""

    OWNER_ONLY = "owner_only"


class PrivateAuthMigrationFailureCode(StrEnum):
    """Secret-safe failure vocabulary for private-auth preparation."""

    UNSAFE_HOME = "unsafe_home"
    SOURCE_MISSING = "source_missing"
    SOURCE_INVALID = "source_invalid"
    SOURCE_IDENTITY_MISMATCH = "source_identity_mismatch"
    TARGET_PARTIAL = "target_partial"
    TARGET_CONFLICT = "target_conflict"
    TARGET_COLLISION = "target_collision"
    OBSERVATION_CONFLICT = "observation_conflict"


@dataclass(frozen=True, slots=True)
class PrivateAuthBundleSnapshot:
    """Exact bounded bundle observation supplied by persistence.

    :param home: Absolute observed bundle home.
    :param present: Whether the bundle directory was proven present.
    :param files: Exact observed file bytes, hidden from representations.
    """

    home: Path = field(repr=False)
    present: bool
    files: Mapping[str, bytes] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.home.is_absolute():
            raise ValueError("Private-auth observation home must be absolute.")
        if type(self.present) is not bool:
            raise TypeError("Private-auth presence must be Boolean.")
        owned = dict(sorted(self.files.items()))
        if not self.present and owned:
            raise ValueError("An absent private-auth bundle has no files.")
        if owned:
            validated = PreparedPrivateBundleWrite(
                path=self.home,
                files=owned,
                expected_bundle_present=self.present,
            )
            object.__setattr__(self, "files", validated.files)
        else:
            object.__setattr__(self, "files", MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class PrivateAuthMigrationRequest:
    """Complete read-only input for one private-auth preparation pass."""

    accounts: tuple[Account, ...] = field(repr=False)
    source_root: Path = field(repr=False)
    source_kind: PrivateAuthHomeKind
    target_root: Path = field(repr=False)
    target_kind: PrivateAuthHomeKind
    bundles: tuple[PrivateAuthBundleSnapshot, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.source_root.is_absolute():
            raise ValueError("Source private-auth root must be absolute.")
        if not self.target_root.is_absolute():
            raise ValueError("Target private-auth root must be absolute.")
        if not isinstance(
            self.source_kind, PrivateAuthHomeKind
        ) or not isinstance(
            self.target_kind,
            PrivateAuthHomeKind,
        ):
            raise TypeError("Private-auth root kinds are invalid.")
        if {self.source_kind, self.target_kind} != {
            PrivateAuthHomeKind.COMPATIBILITY,
            PrivateAuthHomeKind.CANONICAL,
        }:
            raise ValueError(
                "Private-auth migration must connect compatibility and "
                "canonical roots."
            )


@dataclass(frozen=True, slots=True)
class PrivateAuthAccountAssessment:
    """Safe ownership and copy assessment for one account."""

    label: AccountLabel
    kind: PrivateAuthHomeKind
    copy_required: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.label, AccountLabel):
            raise TypeError("Private-auth assessment label is invalid.")
        if not isinstance(self.kind, PrivateAuthHomeKind):
            raise TypeError("Private-auth assessment kind is invalid.")
        if type(self.copy_required) is not bool:
            raise TypeError("Private-auth copy requirement must be Boolean.")
        if self.copy_required and self.kind not in {
            PrivateAuthHomeKind.COMPATIBILITY,
            PrivateAuthHomeKind.CANONICAL,
        }:
            raise ValueError(
                "Only Sidekick-owned private-auth homes require copies."
            )


@dataclass(frozen=True, slots=True)
class PrivateAuthMigrationAssessment:
    """Immutable per-account private-auth migration assessment."""

    accounts: tuple[PrivateAuthAccountAssessment, ...]

    def __post_init__(self) -> None:
        labels = tuple(account.label for account in self.accounts)
        if len(labels) != len(set(labels)):
            raise ValueError("Private-auth assessment labels must be unique.")

    @property
    def copies_required(self) -> int:
        """Return the number of private bundles requiring publication."""
        return sum(account.copy_required for account in self.accounts)


@dataclass(frozen=True, slots=True)
class PreparedPrivateAuthCopy:
    """One exact owner-only private bundle awaiting coordination."""

    account_label: AccountLabel
    relative_path: str
    bundle: PreparedPrivateBundleWrite = field(repr=False)
    permission: PrivateAuthPermission = PrivateAuthPermission.OWNER_ONLY

    def __post_init__(self) -> None:
        if not isinstance(self.account_label, AccountLabel):
            raise TypeError("Private-auth copy label is invalid.")
        private_bundle_relative_components(self.relative_path)
        if self.permission is not PrivateAuthPermission.OWNER_ONLY:
            raise ValueError("Private-auth copies require owner-only access.")


@dataclass(frozen=True, slots=True)
class PreparedPrivateAuthMigration:
    """Deterministic rewritten accounts and exact private bundle copies."""

    accounts: tuple[Account, ...] = field(repr=False)
    assessment: PrivateAuthMigrationAssessment
    copies: tuple[PreparedPrivateAuthCopy, ...] = field(repr=False)
    semantic_digest: Sha256Digest = field(repr=False)

    def __post_init__(self) -> None:
        if len(self.accounts) != len(self.assessment.accounts):
            raise ValueError("Every rewritten account requires an assessment.")
        account_labels = tuple(account.label for account in self.accounts)
        assessment_labels = tuple(
            account.label for account in self.assessment.accounts
        )
        if account_labels != assessment_labels:
            raise ValueError(
                "Rewritten accounts do not align with their assessments."
            )
        copy_labels = tuple(copy.account_label for copy in self.copies)
        required_labels = tuple(
            account.label
            for account in self.assessment.accounts
            if account.copy_required
        )
        if len(copy_labels) != len(set(copy_labels)) or set(
            copy_labels
        ) != set(required_labels):
            raise ValueError("Prepared copies do not align with assessments.")
        if not isinstance(self.semantic_digest, Sha256Digest):
            raise TypeError("Private-auth semantic digest is invalid.")
        relative = tuple(copy.relative_path for copy in self.copies)
        require_portable_unique_private_bundle_paths(relative)

    @property
    def private_bundles(self) -> tuple[PreparedPrivateBundleWrite, ...]:
        """Return transaction-ready private bundle writes."""
        return tuple(copy.bundle for copy in self.copies)


@dataclass(frozen=True, slots=True, kw_only=True)
class PrivateAuthMigrationFailure:
    """Secret-safe typed provider-auth migration failure."""

    code: PrivateAuthMigrationFailureCode
    message: str
    accounts: tuple[AccountLabel, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.code, PrivateAuthMigrationFailureCode):
            raise TypeError("Private-auth failure code is invalid.")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("Private-auth failure message must not be empty.")
        try:
            encoded = self.message.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError(
                "Private-auth failure message must be valid UTF-8."
            ) from None
        if len(encoded) > _MAX_PRIVATE_AUTH_FAILURE_MESSAGE_BYTES:
            raise ValueError("Private-auth failure message is too long.")
        if any(
            unicodedata.category(character) == "Cc"
            for character in self.message
        ):
            raise ValueError(
                "Private-auth failure message contains control characters."
            )
        if any(not isinstance(label, AccountLabel) for label in self.accounts):
            raise TypeError("Private-auth failure labels are invalid.")
        labels = tuple(sorted(set(self.accounts), key=str))
        if len(labels) > MAX_ACCOUNTS:
            raise ValueError("Private-auth failure has too many labels.")
        object.__setattr__(self, "accounts", labels)


type PrivateAuthMigrationResult = (
    PreparedPrivateAuthMigration | PrivateAuthMigrationFailure
)


class PrivateAuthMigrator(Protocol):
    """Validate and prepare provider auth relocation without writing."""

    def prepare(
        self,
        request: PrivateAuthMigrationRequest,
    ) -> PrivateAuthMigrationResult:
        """Return deterministic prepared work or one safe failure."""

"""Provider-neutral private credential artifact persistence boundary."""

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from sidekick_usages.persistence._platform import (
    NativeFailureKind,
    NativeFilesystemError,
)

if sys.platform == "darwin":
    from sidekick_usages.persistence._platform.macos import MacOSPlatform
    from sidekick_usages.persistence._platform.posix_private import (
        PosixPrivateCredentialPlatform,
    )
elif sys.platform == "win32":
    from sidekick_usages.persistence._platform.windows_private import (
        WindowsPrivateCredentialPlatform,
    )
elif sys.platform.startswith("linux"):
    from sidekick_usages.persistence._platform.posix import PosixPlatform
    from sidekick_usages.persistence._platform.posix_private import (
        PosixPrivateCredentialPlatform,
    )
from sidekick_usages.persistence.artifacts import (
    ExpectedAuthority,
    FileFingerprint,
    FileSnapshot,
    require_portable_unique_basenames,
    require_safe_basename,
)
from sidekick_usages.persistence.errors import (
    CandidateWriteError,
    DurabilityUncertainError,
    ManagedFileReadError,
    PersistenceFilesystemError,
    PrivateCredentialArtifactError,
    PrivateCredentialCollisionError,
    PrivateCredentialRepairError,
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.inventory import OrphanedPrivateCredentials
from sidekick_usages.persistence.locking import PersistenceLock


class PrivateCredentialArtifacts(Protocol):
    """Sidekick-owned credential artifacts used by reset coordination."""

    def observe(self) -> OrphanedPrivateCredentials:
        """Return closed presence evidence or fail without guessing."""

    def destroy_all(self) -> None:
        """Delete every private credential artifact and verify removal."""

    def repair_permissions(
        self,
        *,
        locked_precondition: Callable[[], None],
    ) -> PrivateCredentialRepairResult:
        """Repair a released tree under the shared account lock."""


class PrivateCredentialOwnership(StrEnum):
    """Closed lexical ownership classes for a requested private bundle."""

    CANONICAL = "canonical"
    EXISTING_COMPATIBILITY = "existing_compatibility"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class PrivateCredentialRepairResult:
    """Verified outcome of one explicit private-permission repair."""

    root: Path
    account_parent_repaired: bool
    directories_repaired: int
    files_repaired: int
    artifacts_present: bool

    def __post_init__(self) -> None:
        if not self.root.is_absolute():
            raise ValueError(
                "Private credential repair root must be absolute."
            )
        if type(self.account_parent_repaired) is not bool:
            raise TypeError("account_parent_repaired must be Boolean.")
        if self.directories_repaired < 0 or self.files_repaired < 0:
            raise ValueError(
                "Private credential repair counts cannot be negative."
            )
        if type(self.artifacts_present) is not bool:
            raise TypeError("artifacts_present must be Boolean.")


class _PrivateCredentialPlatform(Protocol):
    """Native private-tree operations hidden behind the public facade."""

    def contains_artifacts(self, root: Path) -> bool:
        """Return whether a fully validated private tree has descendants."""

    def ensure_directory(self, path: Path) -> None:
        """Create or validate one protected private directory."""

    def repair_permissions(self, root: Path) -> tuple[int, int]:
        """Preflight and repair a private tree without changing bytes."""

    def destroy_artifacts(self, root: Path) -> None:
        """Delete a fully validated private tree bottom-up."""

    def destroy_tree(self, root: Path) -> None:
        """Delete a fully validated private tree and exact root."""


type _FilesystemFactory = Callable[[Path], PersistenceFilesystem]

PRIVATE_TRANSACTION_DIRECTORY = ".credential-transaction"
PRIVATE_TRANSACTION_JOURNAL = "journal.json"
_MAX_PRIVATE_FILES = 16
_MAX_PRIVATE_FILE_BYTES = 1024 * 1024
_MAX_PRIVATE_BUNDLE_BYTES = 4 * 1024 * 1024


def _validated_private_payloads(
    files: Mapping[str, bytes],
    expected_files: Mapping[str, bytes | None],
) -> tuple[dict[str, bytes], dict[str, bytes | None]]:
    """Validate and own one bounded private-bundle payload set."""
    owned_files = dict(files)
    owned_expected = dict(expected_files)
    if not owned_files or len(owned_files) > _MAX_PRIVATE_FILES:
        raise ValueError("Private credential file count is unsupported.")
    require_portable_unique_basenames(owned_files)
    total = 0
    for basename, payload in owned_files.items():
        require_safe_basename(basename)
        if not isinstance(payload, bytes):
            raise TypeError("Private credential payloads must be bytes.")
        if len(payload) > _MAX_PRIVATE_FILE_BYTES:
            raise ValueError("A private credential file is too large.")
        total += len(payload)
    if total > _MAX_PRIVATE_BUNDLE_BYTES:
        raise ValueError("Private credential bundle is too large.")
    for basename, payload in owned_expected.items():
        require_safe_basename(basename)
        if payload is not None and not isinstance(payload, bytes):
            raise TypeError("Expected private payloads must be bytes.")
        if payload is not None and len(payload) > _MAX_PRIVATE_FILE_BYTES:
            raise ValueError(
                "An expected private credential file is too large."
            )
    if not owned_expected.keys() <= owned_files.keys():
        raise ValueError("Expected files must belong to the prepared bundle.")
    return owned_files, owned_expected


@dataclass(frozen=True, slots=True)
class PreparedPrivateBundleWrite:
    """Secret-safe immutable input for one coordinated private write."""

    path: Path
    files: Mapping[str, bytes] = field(repr=False)
    expected_bundle_present: bool
    expected_files: Mapping[str, bytes | None] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError(
                "Private credential bundle path must be absolute."
            )
        require_safe_basename(self.path.name)
        if type(self.expected_bundle_present) is not bool:
            raise TypeError("expected_bundle_present must be Boolean.")
        files, expected = _validated_private_payloads(
            self.files,
            self.expected_files,
        )
        object.__setattr__(self, "files", MappingProxyType(files))
        object.__setattr__(self, "expected_files", MappingProxyType(expected))


def _current_platform() -> _PrivateCredentialPlatform:
    if sys.platform == "darwin":
        return PosixPrivateCredentialPlatform(MacOSPlatform())
    if sys.platform == "win32":
        return WindowsPrivateCredentialPlatform()
    if sys.platform.startswith("linux"):
        return PosixPrivateCredentialPlatform(PosixPlatform())
    raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)


def _passive_error(
    error: NativeFilesystemError,
    basename: str,
) -> PersistenceFilesystemError:
    """Translate native observation failure without operation history."""
    if error.kind is NativeFailureKind.UNSUPPORTED:
        return UnsupportedFilesystemError(basename)
    if error.kind in {NativeFailureKind.UNSAFE, NativeFailureKind.CHANGED}:
        return UnsafeManagedFileError(basename)
    return ManagedFileReadError(basename)


class PrivateCredentialTree:
    """Secure private-tree adapter bound to one Sidekick-owned root."""

    def __init__(
        self,
        root: Path,
        *,
        account_path: Path | None = None,
        existing_root: Path | None = None,
        _native: _PrivateCredentialPlatform | None = None,
        _filesystem_factory: _FilesystemFactory = PersistenceFilesystem,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("Private credential root must be absolute.")
        require_safe_basename(root.name)
        if account_path is not None:
            if not account_path.is_absolute():
                raise ValueError("Account authority path must be absolute.")
            require_safe_basename(account_path.name)
        if existing_root is not None:
            if not existing_root.is_absolute():
                raise ValueError("Existing credential root must be absolute.")
            require_safe_basename(existing_root.name)
        self.root = root
        self._account_path = account_path
        self._existing_root = existing_root or root
        self._filesystem_factory = _filesystem_factory
        try:
            self._native = _native or _current_platform()
        except NativeFilesystemError as error:
            raise _passive_error(error, root.name) from None

    @property
    def transaction_directory(self) -> Path:
        """Return the reserved private transaction directory."""
        return self.root / PRIVATE_TRANSACTION_DIRECTORY

    def observe(self) -> OrphanedPrivateCredentials:
        """Return safely proven private credential presence."""
        try:
            if os.path.lexists(self.transaction_directory):
                self._native.contains_artifacts(self.transaction_directory)
                return OrphanedPrivateCredentials.INTERRUPTED
            present = self._native.contains_artifacts(self.root)
        except NativeFilesystemError as error:
            raise _passive_error(error, self.root.name) from None
        return (
            OrphanedPrivateCredentials.PRESENT
            if present
            else OrphanedPrivateCredentials.ABSENT
        )

    def destroy_all(self) -> None:
        """Delete all validated artifacts and immediately rescan the root."""
        try:
            self._native.destroy_artifacts(self.root)
            if self._native.contains_artifacts(self.root):
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
        except NativeFilesystemError:
            raise PrivateCredentialArtifactError from None

    def classify_bundle(self, bundle_path: Path) -> PrivateCredentialOwnership:
        """Classify a requested bundle without following filesystem paths."""
        if not bundle_path.is_absolute():
            return PrivateCredentialOwnership.EXTERNAL
        if bundle_path.parent == self.root:
            require_safe_basename(bundle_path.name)
            if bundle_path.name == PRIVATE_TRANSACTION_DIRECTORY:
                return PrivateCredentialOwnership.EXTERNAL
            return PrivateCredentialOwnership.CANONICAL
        if bundle_path.parent == self._existing_root:
            require_safe_basename(bundle_path.name)
            return PrivateCredentialOwnership.EXISTING_COMPATIBILITY
        return PrivateCredentialOwnership.EXTERNAL

    def read_bundle_file(
        self,
        bundle_path: Path,
        basename: str,
    ) -> bytes | None:
        """Read one proven canonical bundle file without mutation."""
        if (
            self.classify_bundle(bundle_path)
            is not PrivateCredentialOwnership.CANONICAL
        ):
            raise ValueError("Private bundle is not canonically owned.")
        require_safe_basename(basename)
        snapshot = self._filesystem_factory(
            bundle_path / basename
        ).read_opaque_private()
        return None if snapshot is None else snapshot.data

    def bundle_present(self, bundle_path: Path) -> bool:
        """Return whether a proven canonical bundle has any descendants."""
        if (
            self.classify_bundle(bundle_path)
            is not PrivateCredentialOwnership.CANONICAL
        ):
            raise ValueError("Private bundle is not canonically owned.")
        try:
            return self._native.contains_artifacts(bundle_path)
        except NativeFilesystemError as error:
            raise _passive_error(error, bundle_path.name) from None

    def transaction_directory_present(self) -> bool:
        """Return whether any reserved transaction directory entry exists."""
        return os.path.lexists(self.transaction_directory)

    def transaction_artifacts_present(self) -> bool:
        """Validate and report descendants of the transaction directory."""
        if not self.transaction_directory_present():
            return False
        try:
            return self._native.contains_artifacts(self.transaction_directory)
        except NativeFilesystemError as error:
            raise _passive_error(
                error,
                PRIVATE_TRANSACTION_DIRECTORY,
            ) from None

    def ensure_transaction_directory(self) -> None:
        """Create or validate the reserved private transaction directory."""
        try:
            self._native.ensure_directory(self.root)
            self._native.ensure_directory(self.transaction_directory)
        except NativeFilesystemError as error:
            if error.kind in {
                NativeFailureKind.UNSUPPORTED,
                NativeFailureKind.UNSAFE,
                NativeFailureKind.UNREADABLE,
                NativeFailureKind.CHANGED,
            }:
                raise _passive_error(
                    error,
                    PRIVATE_TRANSACTION_DIRECTORY,
                ) from None
            raise CandidateWriteError(PRIVATE_TRANSACTION_DIRECTORY) from None

    def read_owned_file(
        self,
        directory: Path,
        basename: str,
    ) -> FileSnapshot | None:
        """Read one exact file from a direct owned private directory."""
        self._require_owned_directory(directory)
        require_safe_basename(basename)
        try:
            directory_has_artifacts = self._native.contains_artifacts(
                directory
            )
        except NativeFilesystemError as error:
            raise _passive_error(error, directory.name) from None
        if not directory_has_artifacts:
            return None
        return self._filesystem_factory(
            directory / basename
        ).read_opaque_private()

    def write_owned_file(
        self,
        directory: Path,
        basename: str,
        payload: bytes,
        *,
        expected_source: ExpectedAuthority | None = None,
    ) -> FileSnapshot:
        """Commit one exact file while the caller holds the account lock."""
        self._require_owned_directory(directory)
        require_safe_basename(basename)
        self._ensure_owned_directory(directory)
        return self._filesystem_factory(
            directory / basename
        ).commit_opaque_private(
            payload,
            expected_source=expected_source,
        )

    def delete_owned_file(
        self,
        directory: Path,
        basename: str,
        expected: FileFingerprint,
    ) -> None:
        """Delete one exact private file under the shared account lock."""
        self._require_owned_directory(directory)
        require_safe_basename(basename)
        self._filesystem_factory(directory / basename).delete_opaque_private(
            expected
        )

    def destroy_owned_directory(self, directory: Path) -> None:
        """Delete all secrets in one direct owned directory, then its leaf."""
        self._require_owned_directory(directory)
        try:
            self._native.destroy_tree(directory)
            if os.path.lexists(directory):
                raise NativeFilesystemError(NativeFailureKind.CHANGED)
        except NativeFilesystemError:
            raise PrivateCredentialArtifactError from None

    def _require_owned_directory(self, directory: Path) -> None:
        if directory == self.transaction_directory:
            return
        if (
            self.classify_bundle(directory)
            is PrivateCredentialOwnership.CANONICAL
        ):
            return
        raise ValueError("Private directory is not canonically owned.")

    def _ensure_owned_directory(self, directory: Path) -> None:
        try:
            self._native.ensure_directory(self.root)
            self._native.ensure_directory(directory)
        except NativeFilesystemError as error:
            if error.kind in {
                NativeFailureKind.UNSUPPORTED,
                NativeFailureKind.UNSAFE,
                NativeFailureKind.UNREADABLE,
                NativeFailureKind.CHANGED,
            }:
                raise _passive_error(error, directory.name) from None
            raise CandidateWriteError(directory.name) from None

    def _validate_bundle_write(
        self,
        bundle_path: Path,
        files: Mapping[str, bytes],
        expected_files: Mapping[str, bytes | None],
    ) -> None:
        if (
            self.classify_bundle(bundle_path)
            is not PrivateCredentialOwnership.CANONICAL
        ):
            raise ValueError("Private bundle is not canonically owned.")
        if not files:
            raise ValueError("Private credential bundle must not be empty.")
        for basename, payload in files.items():
            require_safe_basename(basename)
            if not isinstance(payload, bytes):
                raise TypeError("Private credential payloads must be bytes.")
        for basename, expected in expected_files.items():
            require_safe_basename(basename)
            if expected is not None and not isinstance(expected, bytes):
                raise TypeError("Expected private payloads must be bytes.")
        if not expected_files.keys() <= files.keys():
            raise ValueError(
                "Expected files must belong to the written bundle."
            )

    def _ensure_bundle_directories(self, bundle_path: Path) -> None:
        try:
            self._native.ensure_directory(self.root)
            self._native.ensure_directory(bundle_path)
        except NativeFilesystemError as error:
            if error.kind in {
                NativeFailureKind.UNSUPPORTED,
                NativeFailureKind.UNSAFE,
                NativeFailureKind.UNREADABLE,
                NativeFailureKind.CHANGED,
            }:
                raise _passive_error(error, bundle_path.name) from None
            raise CandidateWriteError(bundle_path.name) from None

    def _require_expected_bundle_files(
        self,
        bundle_path: Path,
        expected_files: Mapping[str, bytes | None],
    ) -> None:
        for basename, expected in expected_files.items():
            snapshot = self._filesystem_factory(
                bundle_path / basename
            ).read_opaque_private()
            if (snapshot is None) is not (expected is None) or (
                snapshot is not None and snapshot.data != expected
            ):
                raise PrivateCredentialCollisionError(bundle_path.name)

    def _commit_and_prove_bundle(
        self,
        bundle_path: Path,
        files: Mapping[str, bytes],
    ) -> None:
        committed: dict[str, FileSnapshot] = {}
        for basename, payload in sorted(files.items()):
            committed[basename] = self._filesystem_factory(
                bundle_path / basename
            ).commit_opaque_private(payload)
        for basename, payload in sorted(files.items()):
            final = self._filesystem_factory(
                bundle_path / basename
            ).read_opaque_private()
            if (
                final is None
                or final.data != payload
                or final.fingerprint.identity
                != committed[basename].fingerprint.identity
            ):
                raise DurabilityUncertainError(bundle_path.name)

    def _require_written_bundle_presence(self, bundle_path: Path) -> None:
        try:
            observed = self.observe()
        except PersistenceFilesystemError:
            raise DurabilityUncertainError(bundle_path.name) from None
        if observed is not OrphanedPrivateCredentials.PRESENT:
            raise DurabilityUncertainError(bundle_path.name)

    def write_bundle(
        self,
        bundle_path: Path,
        files: Mapping[str, bytes],
        *,
        expected_bundle_present: bool,
        expected_files: Mapping[str, bytes | None],
    ) -> Path:
        """Durably write one private bundle under the account lock."""
        if type(expected_bundle_present) is not bool:
            raise TypeError("expected_bundle_present must be Boolean.")
        self._validate_bundle_write(bundle_path, files, expected_files)
        if self._account_path is None:
            raise RuntimeError("Private writes require an account lock path.")
        lock_filesystem = self._filesystem_factory(self._account_path)
        with PersistenceLock(lock_filesystem).hold():
            self._ensure_bundle_directories(bundle_path)
            if self.bundle_present(bundle_path) is not expected_bundle_present:
                raise PrivateCredentialCollisionError(bundle_path.name)
            self._require_expected_bundle_files(bundle_path, expected_files)
            self._commit_and_prove_bundle(bundle_path, files)
            self._require_written_bundle_presence(bundle_path)
            return bundle_path

    def repair_permissions(
        self,
        *,
        locked_precondition: Callable[[], None],
    ) -> PrivateCredentialRepairResult:
        """Explicitly repair a preflight-safe tree under the account lock."""
        if self._account_path is None:
            raise RuntimeError("Private repair requires an account lock path.")
        lock_filesystem = self._filesystem_factory(self._account_path)
        account_parent_repaired = lock_filesystem.repair_parent_permissions()
        with PersistenceLock(lock_filesystem).hold():
            locked_precondition()
            account_parent_repaired = (
                lock_filesystem.repair_parent_permissions()
                or account_parent_repaired
            )
            try:
                directories, files = self._native.repair_permissions(self.root)
            except NativeFilesystemError as error:
                if error.kind in {
                    NativeFailureKind.UNSUPPORTED,
                    NativeFailureKind.UNSAFE,
                    NativeFailureKind.UNREADABLE,
                    NativeFailureKind.CHANGED,
                }:
                    raise _passive_error(error, self.root.name) from None
                if error.kind in {
                    NativeFailureKind.SYNCHRONIZE,
                    NativeFailureKind.HARDEN,
                }:
                    raise DurabilityUncertainError(self.root.name) from None
                raise PrivateCredentialRepairError(self.root.name) from None
            try:
                observed = self.observe()
            except PersistenceFilesystemError:
                raise DurabilityUncertainError(self.root.name) from None
            return PrivateCredentialRepairResult(
                root=self.root,
                account_parent_repaired=account_parent_repaired,
                directories_repaired=directories,
                files_repaired=files,
                artifacts_present=(
                    observed is OrphanedPrivateCredentials.PRESENT
                ),
            )


__all__ = [
    "PreparedPrivateBundleWrite",
    "PrivateCredentialArtifacts",
    "PrivateCredentialOwnership",
    "PrivateCredentialRepairResult",
    "PrivateCredentialTree",
]

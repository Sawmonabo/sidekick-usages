"""Provider-neutral private credential artifact persistence boundary."""

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType

from sidekick_usages.persistence._platform import (
    NativeFailureKind,
    NativeFile,
    NativeFilesystemError,
)

if sys.platform == "darwin":
    from sidekick_usages.persistence._platform.macos import MacOSPlatform
    from sidekick_usages.persistence._platform.posix_private_bundles import (
        PosixPrivateBundlePlatform,
    )
    from sidekick_usages.persistence._platform.posix_private_platform import (
        PosixPrivateCredentialPlatform,
    )
elif sys.platform == "win32":
    from sidekick_usages.persistence._platform.windows_private import (
        WindowsPrivateCredentialPlatform,
    )
    from sidekick_usages.persistence._platform.windows_private_bundles import (
        WindowsPrivateBundlePlatform,
    )
elif sys.platform.startswith("linux"):
    from sidekick_usages.persistence._platform.posix import PosixPlatform
    from sidekick_usages.persistence._platform.posix_private_bundles import (
        PosixPrivateBundlePlatform,
    )
    from sidekick_usages.persistence._platform.posix_private_platform import (
        PosixPrivateCredentialPlatform,
    )
from sidekick_usages.persistence.artifacts import require_safe_basename
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
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileFingerprint,
    FileIdentity,
    FileSnapshot,
)
from sidekick_usages.persistence.models.credential import (
    PrivateCredentialRepairResult,
)
from sidekick_usages.persistence.private_bundle_paths import (
    MAX_PRIVATE_BUNDLE_COMPONENT_BYTES,
    MAX_PRIVATE_BUNDLE_COMPONENTS,
    MAX_PRIVATE_BUNDLE_PATH_BYTES,
    PRIVATE_TRANSACTION_DIRECTORY,
    portable_private_bundle_path_key,
    private_bundle_relative_components,
    require_portable_unique_private_bundle_paths,
)
from sidekick_usages.persistence.private_bundle_writes import (
    MAX_PRIVATE_BUNDLE_BYTES,
    MAX_PRIVATE_FILE_BYTES,
    MAX_PRIVATE_FILES,
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private_credential_contracts import (
    PrivateBundleNative,
    PrivateCredentialArtifacts,
    PrivateCredentialNative,
)
from sidekick_usages.persistence.types.artifact import (
    AuthorityExpectation,
    sha256_digest,
)
from sidekick_usages.persistence.types.credential import (
    PrivateCredentialOwnership,
    PrivateCredentialState,
)

__all__ = [
    "MAX_PRIVATE_BUNDLE_COMPONENTS",
    "MAX_PRIVATE_BUNDLE_COMPONENT_BYTES",
    "MAX_PRIVATE_BUNDLE_PATH_BYTES",
    "PreparedPrivateBundleWrite",
    "PrivateCredentialArtifacts",
    "PrivateCredentialTree",
    "portable_private_bundle_path_key",
    "private_bundle_relative_components",
    "require_portable_unique_private_bundle_paths",
]

type _FilesystemFactory = Callable[[Path], PersistenceFilesystem]


def _current_platform() -> PrivateCredentialNative:
    if sys.platform == "darwin":
        return PosixPrivateCredentialPlatform(MacOSPlatform())
    if sys.platform == "win32":
        return WindowsPrivateCredentialPlatform()
    if sys.platform.startswith("linux"):
        return PosixPrivateCredentialPlatform(PosixPlatform())
    raise NativeFilesystemError(NativeFailureKind.UNSUPPORTED)


def _current_bundle_platform() -> PrivateBundleNative:
    if sys.platform == "darwin":
        return PosixPrivateBundlePlatform(MacOSPlatform())
    if sys.platform == "win32":
        return WindowsPrivateBundlePlatform()
    if sys.platform.startswith("linux"):
        return PosixPrivateBundlePlatform(PosixPlatform())
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
        _native: PrivateCredentialNative | None = None,
        _bundle_native: PrivateBundleNative | None = None,
        _filesystem_factory: _FilesystemFactory = PersistenceFilesystem,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("Private credential root must be absolute.")
        require_safe_basename(root.name)
        if account_path is not None:
            if not account_path.is_absolute():
                raise ValueError("Account authority path must be absolute.")
            require_safe_basename(account_path.name)
        self.root = root
        self._account_path = account_path
        self._filesystem_factory = _filesystem_factory
        try:
            self._native = _native or _current_platform()
            self._bundle_native = _bundle_native or _current_bundle_platform()
        except NativeFilesystemError as error:
            raise _passive_error(error, root.name) from None

    @property
    def transaction_directory(self) -> Path:
        """Return the reserved private transaction directory."""
        return self.root / PRIVATE_TRANSACTION_DIRECTORY

    def observe(self) -> PrivateCredentialState:
        """Return safely proven private credential presence."""
        try:
            if os.path.lexists(self.transaction_directory):
                self._native.contains_artifacts(self.transaction_directory)
                return PrivateCredentialState.INTERRUPTED
            present = self._native.contains_artifacts(self.root)
        except NativeFilesystemError as error:
            raise _passive_error(error, self.root.name) from None
        return (
            PrivateCredentialState.PRESENT
            if present
            else PrivateCredentialState.ABSENT
        )

    def list_owned_directories(self) -> tuple[Path, ...]:
        """Return direct directory children after a complete secure scan."""
        try:
            basenames = self._native.list_directories(self.root)
        except NativeFilesystemError as error:
            raise _passive_error(error, self.root.name) from None
        return tuple(self.root / basename for basename in basenames)

    def list_owned_directories_shallow(self) -> tuple[Path, ...]:
        """Return direct owned directories without inspecting descendants."""
        try:
            basenames = self._native.list_directories_shallow(self.root)
        except NativeFilesystemError as error:
            raise _passive_error(error, self.root.name) from None
        return tuple(self.root / basename for basename in basenames)

    def list_owned_files(self) -> tuple[Path, ...]:
        """Return direct file children after a complete secure scan."""
        try:
            basenames = self._native.list_files(self.root)
        except NativeFilesystemError as error:
            raise _passive_error(error, self.root.name) from None
        return tuple(self.root / basename for basename in basenames)

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
        try:
            canonical_relative = bundle_path.relative_to(self.root)
        except ValueError:
            canonical_relative = None
        if canonical_relative is not None:
            private_bundle_relative_components(canonical_relative.as_posix())
            return PrivateCredentialOwnership.CANONICAL
        return PrivateCredentialOwnership.EXTERNAL

    def relative_bundle_path(self, bundle_path: Path) -> str:
        """Return one validated canonical bundle path relative to the root."""
        if (
            self.classify_bundle(bundle_path)
            is not PrivateCredentialOwnership.CANONICAL
        ):
            raise ValueError("Private bundle is not canonically owned.")
        relative = bundle_path.relative_to(self.root).as_posix()
        private_bundle_relative_components(relative)
        return relative

    def canonical_bundle_path(self, relative: str) -> Path:
        """Reconstruct one validated canonical bundle from journal text."""
        components = private_bundle_relative_components(relative)
        return self.root.joinpath(*components)

    @staticmethod
    def _snapshot(native: NativeFile) -> FileSnapshot:
        return FileSnapshot(
            FileFingerprint(
                FileIdentity(native.device, native.inode),
                sha256_digest(native.data),
                len(native.data),
            ),
            native.link_count,
            native.data,
        )

    def read_relative_bundle_file(
        self,
        relative: str,
        basename: str,
    ) -> FileSnapshot | None:
        """Read one nested bundle file through qualified components."""
        components = private_bundle_relative_components(relative)
        require_safe_basename(basename)
        try:
            native = self._bundle_native.read_relative_file(
                self.root,
                components,
                basename,
                MAX_PRIVATE_FILE_BYTES,
            )
        except NativeFilesystemError as error:
            raise _passive_error(error, basename) from None
        return None if native is None else self._snapshot(native)

    def read_relative_bundle(
        self,
        relative: str,
    ) -> Mapping[str, FileSnapshot] | None:
        """Return a complete immutable direct-file bundle observation."""
        components = private_bundle_relative_components(relative)
        try:
            native = self._bundle_native.read_relative_bundle(
                self.root,
                components,
                MAX_PRIVATE_FILES,
                MAX_PRIVATE_FILE_BYTES,
                MAX_PRIVATE_BUNDLE_BYTES,
            )
        except NativeFilesystemError as error:
            raise _passive_error(error, components[-1]) from None
        if native is None:
            return None
        return MappingProxyType(
            {
                basename: self._snapshot(snapshot)
                for basename, snapshot in native
            }
        )

    def relative_bundle_present(self, relative: str) -> bool:
        """Report one nested bundle's qualified descendant state."""
        components = private_bundle_relative_components(relative)
        try:
            return self._bundle_native.contains_relative_artifacts(
                self.root,
                components,
            )
        except NativeFilesystemError as error:
            raise _passive_error(error, components[-1]) from None

    def install_staged_bundle_file(
        self,
        relative: str,
        basename: str,
        stage_basename: str,
        *,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Install one journal stage through qualified component chains."""
        components = private_bundle_relative_components(relative)
        require_safe_basename(basename)
        require_safe_basename(stage_basename)
        current = self.read_relative_bundle_file(relative, basename)
        if expected_source is AuthorityExpectation.ABSENT:
            if current is not None:
                raise PrivateCredentialCollisionError(components[-1])
        elif current is None or current.fingerprint != expected_source:
            raise PrivateCredentialCollisionError(components[-1])
        expected_native = (
            None
            if current is None
            else NativeFile(
                current.fingerprint.identity.device,
                current.fingerprint.identity.inode,
                current.link_count,
                current.data,
            )
        )
        try:
            native = self._bundle_native.install_staged_file(
                self.root,
                (PRIVATE_TRANSACTION_DIRECTORY,),
                stage_basename,
                components,
                basename,
                expected_native,
                MAX_PRIVATE_FILE_BYTES,
            )
        except NativeFilesystemError as error:
            if error.kind in {
                NativeFailureKind.CHANGED,
                NativeFailureKind.UNSAFE,
                NativeFailureKind.UNREADABLE,
            }:
                raise PrivateCredentialCollisionError(components[-1]) from None
            raise DurabilityUncertainError(components[-1]) from None
        return self._snapshot(native)

    def delete_relative_bundle_file(
        self,
        relative: str,
        basename: str,
        expected: FileFingerprint,
    ) -> None:
        """Delete one exact nested file through qualified components."""
        components = private_bundle_relative_components(relative)
        require_safe_basename(basename)
        current = self.read_relative_bundle_file(relative, basename)
        if current is None or current.fingerprint != expected:
            raise PrivateCredentialCollisionError(components[-1])
        native = NativeFile(
            current.fingerprint.identity.device,
            current.fingerprint.identity.inode,
            current.link_count,
            current.data,
        )
        try:
            self._bundle_native.delete_relative_file(
                self.root,
                components,
                basename,
                native,
                MAX_PRIVATE_FILE_BYTES,
            )
        except NativeFilesystemError:
            raise PrivateCredentialArtifactError from None

    def destroy_relative_bundle(self, relative: str) -> None:
        """Delete one exact nested bundle through qualified components."""
        components = private_bundle_relative_components(relative)
        try:
            self._bundle_native.destroy_relative_tree(self.root, components)
        except NativeFilesystemError:
            raise PrivateCredentialArtifactError from None

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
        if directory == self.transaction_directory:
            try:
                native = self._bundle_native.read_relative_file(
                    self.root,
                    (PRIVATE_TRANSACTION_DIRECTORY,),
                    basename,
                    MAX_PRIVATE_FILE_BYTES,
                )
            except NativeFilesystemError as error:
                raise _passive_error(error, directory.name) from None
            return None if native is None else self._snapshot(native)
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

    def ensure_owned_directory(self, directory: Path) -> None:
        """Create or validate one qualified directory below the root."""
        self._require_owned_directory(directory)
        self._ensure_owned_directory(directory)

    def harden_provider_stage(self, directory: Path) -> None:
        """Normalize only provider output below one held transaction."""
        self._require_owned_directory(directory)
        stage_home = directory / "provider-home"
        try:
            self._native.harden_provider_stage(stage_home)
        except NativeFilesystemError as error:
            raise _passive_error(error, stage_home.name) from None

    def delete_owned_file(
        self,
        directory: Path,
        basename: str,
        expected: FileFingerprint,
    ) -> None:
        """Delete one exact private file under the shared account lock."""
        self._require_owned_directory(directory)
        require_safe_basename(basename)
        if directory == self.transaction_directory:
            current = self.read_owned_file(directory, basename)
            if current is None or current.fingerprint != expected:
                raise PrivateCredentialArtifactError
            native = NativeFile(
                current.fingerprint.identity.device,
                current.fingerprint.identity.inode,
                current.link_count,
                current.data,
            )
            try:
                self._bundle_native.delete_relative_file(
                    self.root,
                    (PRIVATE_TRANSACTION_DIRECTORY,),
                    basename,
                    native,
                    MAX_PRIVATE_FILE_BYTES,
                )
            except NativeFilesystemError:
                raise PrivateCredentialArtifactError from None
            return
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
        if observed is not PrivateCredentialState.PRESENT:
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
                artifacts_present=(observed is PrivateCredentialState.PRESENT),
            )

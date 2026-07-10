"""Secure private credential tree behavior."""

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from sidekick_usages.persistence._platform import (
    NativeFailureKind,
    NativeFile,
    NativeFilesystemError,
    posix_private,
    posix_private_bundles,
)
from sidekick_usages.persistence.artifacts import (
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.errors import (
    DurabilityUncertainError,
    ManagedFileReadError,
    PersistenceFilesystemError,
    PrivateCredentialArtifactError,
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.inventory import OrphanedPrivateCredentials
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialOwnership,
    PrivateCredentialTree,
)

if sys.platform == "win32":
    import win32file

    from sidekick_usages.persistence._platform import (
        windows_private,
        windows_private_tree,
    )
    from sidekick_usages.persistence._platform.windows_security import (
        private_security_attributes,
    )

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_RELEASED_DIRECTORY_MODE = 0o755
_EXPOSED_FILE_MODE = 0o640


@dataclass(slots=True)
class _RecordingPlatform:
    present: bool
    clear_on_destroy: bool
    contains_calls: int = 0
    destroy_calls: int = 0
    failure: NativeFailureKind | None = None

    def ensure_directory(self, path: Path) -> None:
        del path

    def repair_permissions(self, root: Path) -> tuple[int, int]:
        del root
        if self.failure is not None:
            raise NativeFilesystemError(self.failure)
        return (0, 0)

    def contains_artifacts(self, root: Path) -> bool:
        del root
        self.contains_calls += 1
        if self.failure is not None:
            raise NativeFilesystemError(self.failure)
        return self.present

    def destroy_artifacts(self, root: Path) -> None:
        del root
        self.destroy_calls += 1
        if self.failure is not None:
            raise NativeFilesystemError(self.failure)
        if self.clear_on_destroy:
            self.present = False

    def destroy_tree(self, root: Path) -> None:
        """Model complete owned-tree deletion for facade type checks."""
        self.destroy_artifacts(root)


class _TamperingFilesystem(PersistenceFilesystem):
    """Remove the first bundle file after the second file commits."""

    def commit_opaque_private(
        self,
        payload: bytes,
        *,
        expected_source: ExpectedAuthority | None = None,
    ) -> FileSnapshot:
        snapshot = super().commit_opaque_private(
            payload,
            expected_source=expected_source,
        )
        if self.authority_path.name == "config.toml":
            (self.authority_path.parent / "auth.json").unlink()
        return snapshot


def _private_directory(path: Path) -> None:
    path.mkdir()
    path.chmod(_PRIVATE_DIRECTORY_MODE)


def _private_file(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(_PRIVATE_FILE_MODE)


def stat_mode(path: Path) -> int:
    """Return only permission bits for one test path."""
    return stat.S_IMODE(path.stat().st_mode)


def test_facade_maps_native_failure_and_requires_post_delete_absence(
    tmp_path: Path,
) -> None:
    native = _RecordingPlatform(present=True, clear_on_destroy=False)
    tree = PrivateCredentialTree(tmp_path / "codex", _native=native)

    assert tree.observe() is OrphanedPrivateCredentials.PRESENT
    with pytest.raises(PrivateCredentialArtifactError):
        tree.destroy_all()
    assert (native.destroy_calls, native.contains_calls) == (1, 2)


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        (NativeFailureKind.UNSUPPORTED, UnsupportedFilesystemError),
        (NativeFailureKind.UNSAFE, UnsafeManagedFileError),
        (NativeFailureKind.CHANGED, UnsafeManagedFileError),
        (NativeFailureKind.UNREADABLE, ManagedFileReadError),
    ],
)
def test_observe_maps_native_failure_to_passive_vocabulary(
    tmp_path: Path,
    failure: NativeFailureKind,
    error_type: type[Exception],
) -> None:
    native = _RecordingPlatform(
        present=False,
        clear_on_destroy=False,
        failure=failure,
    )

    with pytest.raises(error_type) as raised:
        PrivateCredentialTree(tmp_path / "codex", _native=native).observe()
    assert getattr(raised.value, "artifact_basename", None) == "codex"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor contract")
def test_posix_private_tree_deletes_nested_artifacts_and_nothing_external(
    tmp_path: Path,
) -> None:
    root = tmp_path / "codex"
    tree = PrivateCredentialTree(root)
    assert tree.observe() is OrphanedPrivateCredentials.ABSENT
    _private_directory(root)
    account = root / "team"
    _private_directory(account)
    nested = account / "session"
    _private_directory(nested)
    _private_file(account / "config.toml", b"credential_store = 'file'")
    _private_file(nested / "auth.json", b"test-only-private-secret")
    external = tmp_path / "active-codex-auth.json"
    _private_file(external, b"test-only-active-login")
    assert tree.observe() is OrphanedPrivateCredentials.PRESENT
    tree.destroy_all()

    assert tree.observe() is OrphanedPrivateCredentials.ABSENT
    assert tuple(root.iterdir()) == ()
    assert external.read_bytes() == b"test-only-active-login"


@pytest.mark.skipif(os.name == "nt", reason="POSIX object contract")
def test_posix_private_tree_rejects_unsafe_objects_without_deletion(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external-auth.json"
    _private_file(external, b"test-only-external-secret")

    symlink_root = tmp_path / "symlink-root"
    _private_directory(symlink_root)
    (symlink_root / "auth.json").symlink_to(external)
    symlink_tree = PrivateCredentialTree(symlink_root)
    with pytest.raises(PrivateCredentialArtifactError):
        symlink_tree.destroy_all()
    assert external.read_bytes() == b"test-only-external-secret"

    hardlink_root = tmp_path / "hardlink-root"
    _private_directory(hardlink_root)
    hardlink = hardlink_root / "auth.json"
    hardlink.hardlink_to(external)
    hardlink_tree = PrivateCredentialTree(hardlink_root)
    with pytest.raises(PrivateCredentialArtifactError):
        hardlink_tree.destroy_all()
    assert hardlink.exists()
    assert external.read_bytes() == b"test-only-external-secret"

    permission_root = tmp_path / "permission-root"
    _private_directory(permission_root)
    exposed = permission_root / "auth.json"
    _private_file(exposed, b"test-only-exposed-secret")
    exposed.chmod(0o640)
    with pytest.raises(PrivateCredentialArtifactError):
        PrivateCredentialTree(permission_root).destroy_all()
    assert exposed.read_bytes() == b"test-only-exposed-secret"

    fifo_root = tmp_path / "fifo-root"
    _private_directory(fifo_root)
    fifo = fifo_root / "auth.json"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(PrivateCredentialArtifactError):
        PrivateCredentialTree(fifo_root).destroy_all()
    assert fifo.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX identity contract")
def test_posix_private_tree_preserves_identity_swapped_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "codex"
    _private_directory(root)
    auth = root / "auth.json"
    _private_file(auth, b"test-only-original-secret")
    replacement = tmp_path / "replacement.json"
    _private_file(replacement, b"test-only-swapped-secret")
    original_scan = posix_private._scan_tree
    scanned = False

    def swap_after_preflight(
        opened: posix_private._OpenedTree,
    ) -> tuple[
        tuple[posix_private._TreeEntry, ...],
        dict[posix_private._RelativePath, posix_private._Identity],
    ]:
        nonlocal scanned
        result = original_scan(opened)
        if not scanned:
            scanned = True
            os.replace(replacement, auth)
        return result

    monkeypatch.setattr(posix_private, "_scan_tree", swap_after_preflight)

    with pytest.raises(PrivateCredentialArtifactError):
        PrivateCredentialTree(root).destroy_all()
    assert auth.read_bytes() == b"test-only-swapped-secret"


@pytest.mark.skipif(os.name == "nt", reason="POSIX unlink proof")
def test_posix_private_tree_detects_immediate_file_rename_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "codex"
    _private_directory(root)
    auth = root / "auth.json"
    _private_file(auth, b"test-only-original-secret")
    leaked = root / "renamed-auth.json"
    original_unlink = posix_private.os.unlink
    swapped = False

    def swap_before_unlink(
        basename: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if basename == "auth.json" and not swapped:
            swapped = True
            auth.rename(leaked)
            _private_file(auth, b"test-only-replacement-secret")
        original_unlink(basename, dir_fd=dir_fd)

    monkeypatch.setattr(posix_private.os, "unlink", swap_before_unlink)

    with pytest.raises(PrivateCredentialArtifactError):
        PrivateCredentialTree(root).destroy_all()
    assert leaked.read_bytes() == b"test-only-original-secret"
    assert not auth.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX rmdir proof")
def test_posix_private_tree_detects_immediate_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "codex"
    _private_directory(root)
    bundle = root / "team"
    _private_directory(bundle)
    renamed = root / "renamed-team"
    original_rmdir = posix_private.os.rmdir
    swapped = False

    def swap_before_rmdir(
        basename: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal swapped
        if basename == "team" and not swapped:
            swapped = True
            bundle.rename(renamed)
            _private_directory(bundle)
        original_rmdir(basename, dir_fd=dir_fd)

    monkeypatch.setattr(posix_private.os, "rmdir", swap_before_rmdir)

    with pytest.raises(PrivateCredentialArtifactError):
        PrivateCredentialTree(root).destroy_all()
    assert renamed.is_dir()
    assert not bundle.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_repair_hardens_released_layout_and_preserves_credentials(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "sidekick-usages"
    app_root.mkdir(mode=_RELEASED_DIRECTORY_MODE)
    accounts = app_root / "accounts.json"
    _private_file(accounts, b"test-only-account-authority")
    private_root = app_root / "codex"
    private_root.mkdir(mode=_RELEASED_DIRECTORY_MODE)
    bundle = private_root / "team"
    _private_directory(bundle)
    auth = bundle / "auth.json"
    _private_file(auth, b"test-only-private-secret")
    tree = PrivateCredentialTree(private_root, account_path=accounts)
    locked_checks: list[str] = []

    def require_locked_state() -> None:
        assert stat_mode(app_root) == _PRIVATE_DIRECTORY_MODE
        assert (app_root / "accounts.json.lock").is_file()
        locked_checks.append("checked")

    with pytest.raises(UnsafeManagedFileError):
        tree.observe()
    result = tree.repair_permissions(
        locked_precondition=require_locked_state,
    )

    assert locked_checks == ["checked"]
    assert result.account_parent_repaired is True
    assert result.directories_repaired == 1
    assert result.files_repaired == 0
    assert result.artifacts_present is True
    assert stat_mode(app_root) == _PRIVATE_DIRECTORY_MODE
    assert stat_mode(private_root) == _PRIVATE_DIRECTORY_MODE
    assert stat_mode(bundle) == _PRIVATE_DIRECTORY_MODE
    assert stat_mode(accounts) == _PRIVATE_FILE_MODE
    assert stat_mode(auth) == _PRIVATE_FILE_MODE
    assert accounts.read_bytes() == b"test-only-account-authority"
    assert auth.read_bytes() == b"test-only-private-secret"
    assert (
        PrivateCredentialTree(private_root).observe()
        is OrphanedPrivateCredentials.PRESENT
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_repair_preflight_rejects_exposed_file_before_private_mutation(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "sidekick-usages"
    app_root.mkdir(mode=_RELEASED_DIRECTORY_MODE)
    accounts = app_root / "accounts.json"
    _private_file(accounts, b"test-only-account-authority")
    private_root = app_root / "codex"
    private_root.mkdir(mode=_RELEASED_DIRECTORY_MODE)
    exposed = private_root / "auth.json"
    _private_file(exposed, b"test-only-private-secret")
    exposed.chmod(_EXPOSED_FILE_MODE)
    tree = PrivateCredentialTree(private_root, account_path=accounts)

    with pytest.raises(UnsafeManagedFileError):
        tree.repair_permissions(locked_precondition=lambda: None)

    assert stat_mode(app_root) == _PRIVATE_DIRECTORY_MODE
    assert stat_mode(private_root) == _RELEASED_DIRECTORY_MODE
    assert stat_mode(exposed) == _EXPOSED_FILE_MODE
    assert exposed.read_bytes() == b"test-only-private-secret"


@pytest.mark.skipif(os.name == "nt", reason="POSIX private writer contract")
def test_private_bundle_writer_is_locked_durable_and_freshly_observable(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "sidekick-usages"
    accounts = app_root / "accounts.json"
    private_root = app_root / "codex"
    tree = PrivateCredentialTree(private_root, account_path=accounts)

    bundle = tree.write_bundle(
        private_root / "team",
        {
            "auth.json": b'{"token":"test-only-private-secret"}',
            "config.toml": b'cli_auth_credentials_store = "file"\n',
        },
        expected_bundle_present=False,
        expected_files={"auth.json": None},
    )

    assert bundle == private_root / "team"
    assert stat_mode(app_root) == _PRIVATE_DIRECTORY_MODE
    assert stat_mode(private_root) == _PRIVATE_DIRECTORY_MODE
    assert stat_mode(bundle) == _PRIVATE_DIRECTORY_MODE
    assert stat_mode(bundle / "auth.json") == _PRIVATE_FILE_MODE
    assert stat_mode(bundle / "config.toml") == _PRIVATE_FILE_MODE
    assert (app_root / "accounts.json.lock").is_file()
    assert stat_mode(app_root / "accounts.json.lock") == _PRIVATE_FILE_MODE
    assert (
        PrivateCredentialTree(private_root).observe()
        is OrphanedPrivateCredentials.PRESENT
    )


def test_private_bundle_ownership_distinguishes_compatibility_and_external(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "sidekick-usages" / "codex"
    existing = tmp_path / "legacy-sidekick" / "codex"
    tree = PrivateCredentialTree(canonical, existing_root=existing)

    assert (
        tree.classify_bundle(canonical / "team")
        is PrivateCredentialOwnership.CANONICAL
    )
    assert (
        tree.classify_bundle(existing / "team")
        is PrivateCredentialOwnership.EXISTING_COMPATIBILITY
    )
    assert (
        tree.classify_bundle(tmp_path / "external-codex")
        is PrivateCredentialOwnership.EXTERNAL
    )


def test_relative_bundle_observation_is_complete_and_absence_is_distinct(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sidekick" / "codex"
    tree = PrivateCredentialTree(root, account_path=tmp_path / "accounts.json")
    assert tree.read_relative_bundle("teams/missing") is None

    bundle = tree.write_bundle(
        root / "teams" / "primary",
        {
            "auth.json": b"test-only-private-auth",
            "config.toml": b"test-only-private-config",
        },
        expected_bundle_present=False,
        expected_files={"auth.json": None},
    )
    observed = tree.read_relative_bundle("teams/primary")

    assert observed is not None
    assert {
        basename: snapshot.data for basename, snapshot in observed.items()
    } == {
        "auth.json": b"test-only-private-auth",
        "config.toml": b"test-only-private-config",
    }
    assert bundle == root / "teams" / "primary"


@pytest.mark.parametrize(
    ("invalid", "error_type"),
    [
        ("nested", UnsafeManagedFileError),
        ("symlink", UnsafeManagedFileError),
        ("alias", UnsafeManagedFileError),
        ("too_many", ManagedFileReadError),
    ],
)
@pytest.mark.skipif(os.name == "nt", reason="POSIX invalid namespace fixtures")
def test_relative_bundle_observation_rejects_incomplete_namespaces(
    tmp_path: Path,
    invalid: str,
    error_type: type[PersistenceFilesystemError],
) -> None:
    root = tmp_path / "sidekick" / "codex"
    bundle = root / "teams" / "primary"
    bundle.mkdir(parents=True)
    for path in (root, root / "teams", bundle):
        path.chmod(_PRIVATE_DIRECTORY_MODE)
    _private_file(bundle / "auth.json", b"test-only-private-auth")
    if invalid == "nested":
        _private_directory(bundle / "nested")
    elif invalid == "symlink":
        (bundle / "linked.json").symlink_to(bundle / "auth.json")
    elif invalid == "alias":
        _private_file(bundle / "AUTH.JSON", b"test-only-alias")
        if len(tuple(bundle.iterdir())) == 1:
            pytest.skip("host filesystem collapses case aliases")
    else:
        for index in range(16):
            _private_file(bundle / f"extra-{index:02d}.json", b"x")

    with pytest.raises(error_type):
        PrivateCredentialTree(root).read_relative_bundle("teams/primary")


@pytest.mark.skipif(os.name == "nt", reason="POSIX component identity swap")
def test_relative_bundle_observation_rejects_intermediate_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "sidekick" / "codex"
    teams = root / "teams"
    bundle = teams / "primary"
    bundle.mkdir(parents=True)
    for path in (root, teams, bundle):
        path.chmod(_PRIVATE_DIRECTORY_MODE)
    _private_file(bundle / "auth.json", b"test-only-private-auth")
    original = posix_private_bundles._read_bundle_pass
    swapped = False

    def swap_after_read(
        opened: posix_private._OpenedTree,
        chain: posix_private_bundles._OpenedChain,
        names: tuple[str, ...],
        file_limit: int,
        total_limit: int,
    ) -> tuple[tuple[str, NativeFile], ...]:
        nonlocal swapped
        result = original(opened, chain, names, file_limit, total_limit)
        if not swapped:
            swapped = True
            teams.rename(root / "original-teams")
            replacement = root / "teams"
            replacement.mkdir()
            replacement.chmod(_PRIVATE_DIRECTORY_MODE)
        return result

    monkeypatch.setattr(
        posix_private_bundles,
        "_read_bundle_pass",
        swap_after_read,
    )

    with pytest.raises(UnsafeManagedFileError):
        PrivateCredentialTree(root).read_relative_bundle("teams/primary")


@pytest.mark.skipif(os.name == "nt", reason="POSIX tamper fixture")
def test_private_bundle_final_proof_rejects_between_file_removal(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "sidekick-usages"
    accounts = app_root / "accounts.json"
    private_root = app_root / "codex"
    tree = PrivateCredentialTree(
        private_root,
        account_path=accounts,
        _filesystem_factory=_TamperingFilesystem,
    )

    with pytest.raises(DurabilityUncertainError):
        tree.write_bundle(
            private_root / "team",
            {
                "auth.json": b"test-only-private-secret",
                "config.toml": b"test-only-config",
            },
            expected_bundle_present=False,
            expected_files={"auth.json": None},
        )


if sys.platform == "win32":

    def test_windows_relative_bundle_observation_rejects_nested_directory(
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "sidekick" / "codex"
        tree = PrivateCredentialTree(
            root,
            account_path=tmp_path / "sidekick" / "accounts.json",
        )
        bundle = tree.write_bundle(
            root / "teams" / "primary",
            {"auth.json": b"test-only-private-auth"},
            expected_bundle_present=False,
            expected_files={"auth.json": None},
        )
        win32file.CreateDirectoryW(
            str(bundle / "nested"),
            private_security_attributes(directory=True),
        )

        with pytest.raises(UnsafeManagedFileError):
            tree.read_relative_bundle("teams/primary")

    def test_windows_private_tree_uses_handle_deletion_and_rejects_swap(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "codex"
        win32file.CreateDirectoryW(
            str(root),
            private_security_attributes(directory=True),
        )

        def write_private(path: Path, payload: bytes) -> None:
            handle = win32file.CreateFile(
                str(path),
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0,
                private_security_attributes(directory=False),
                win32file.CREATE_NEW,
                stat.FILE_ATTRIBUTE_NORMAL,
                None,
            )
            try:
                win32file.WriteFile(handle, payload)
                win32file.FlushFileBuffers(handle)
            finally:
                handle.Close()

        bundle = root / "team"
        win32file.CreateDirectoryW(
            str(bundle),
            private_security_attributes(directory=True),
        )
        deleted = bundle / "auth.json"
        write_private(deleted, b"test-only-deleted-secret")
        tree = PrivateCredentialTree(root)
        assert tree.observe() is OrphanedPrivateCredentials.PRESENT
        tree.destroy_all()
        assert tree.observe() is OrphanedPrivateCredentials.ABSENT
        assert not deleted.exists()
        assert not bundle.exists()

        auth = root / "auth.json"
        replacement = tmp_path / "replacement.json"
        write_private(auth, b"test-only-original-secret")
        write_private(replacement, b"test-only-swapped-secret")
        original_scan = windows_private._scan_tree
        scanned = False

        def swap_after_preflight(
            opened: windows_private_tree.OpenedTree,
        ) -> tuple[
            tuple[windows_private_tree.TreeEntry, ...],
            dict[
                windows_private_tree.RelativePath,
                windows_private_tree.Identity,
            ],
        ]:
            nonlocal scanned
            result = original_scan(opened)
            if not scanned:
                scanned = True
                os.replace(replacement, auth)
            return result

        monkeypatch.setattr(
            windows_private,
            "_scan_tree",
            swap_after_preflight,
        )
        with pytest.raises(PrivateCredentialArtifactError):
            tree.destroy_all()
        assert auth.read_bytes() == b"test-only-swapped-secret"

"""Native security and platform qualification gates for persistence."""

import os
import stat
import sys
from pathlib import Path

import pytest

from sidekick_usages.persistence.platform.errors import NativeFilesystemError
from sidekick_usages.persistence.platform.posix import mounts
from sidekick_usages.persistence.platform.posix.adapter import PosixPlatform
from sidekick_usages.persistence.platform.posix.mounts import (
    _classify_linux_filesystem,
)
from sidekick_usages.persistence.platform.posix.namespace import (
    owned_descriptor,
)
from sidekick_usages.persistence.platform.types import (
    FilesystemFamily,
    NativeFailureKind,
)

if sys.platform == "win32":
    import ntsecuritycon
    import win32api
    import win32con
    import win32file
    import win32security

    from sidekick_usages.persistence.platform.windows import namespace
    from sidekick_usages.persistence.platform.windows.namespace import (
        existing_ancestor,
        qualify_local_ntfs,
    )
    from sidekick_usages.persistence.platform.windows.security import (
        _allowed_sids,
        _validate_acl,
        _validate_external_acl,
    )
else:
    import fcntl

    from sidekick_usages.persistence.platform.macos import adapter
    from sidekick_usages.persistence.platform.macos.adapter import (
        MacOSPlatform,
    )
from sidekick_usages.persistence.errors import (
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.account import VersionThreeDocument
from sidekick_usages.persistence.schema.account import encode_version_three
from sidekick_usages.persistence.types.artifact import AuthorityExpectation

AUTHORITY_PAYLOAD = encode_version_three(VersionThreeDocument(()))
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


@pytest.mark.skipif(os.name == "nt", reason="Linux/WSL mount policy")
def test_linux_and_wsl_filesystem_allowlists_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.setattr(mounts.platform, "release", lambda: "6.8.0-linux")
    assert {
        name: _classify_linux_filesystem(name)
        for name in ("ext4", "xfs", "btrfs")
    } == {
        "ext4": FilesystemFamily.EXT4,
        "xfs": FilesystemFamily.XFS,
        "btrfs": FilesystemFamily.BTRFS,
    }
    for rejected in ("9p", "overlay", "tmpfs", "nfs"):
        with pytest.raises(NativeFilesystemError):
            _classify_linux_filesystem(rejected)

    monkeypatch.setattr(
        mounts.platform,
        "release",
        lambda: "6.6.87.2-microsoft-standard-WSL2",
    )
    assert _classify_linux_filesystem("ext4") is FilesystemFamily.EXT4
    for rejected in ("xfs", "btrfs", "9p"):
        with pytest.raises(NativeFilesystemError):
            _classify_linux_filesystem(rejected)


def test_fresh_tree_is_private_and_committable(tmp_path: Path) -> None:
    authority = tmp_path / "home" / ".config" / "sidekick" / "accounts.json"
    filesystem = PersistenceFilesystem(authority)

    with PersistenceLock(filesystem).hold() as transaction:
        transaction.commit_authority(
            AUTHORITY_PAYLOAD,
            AuthorityExpectation.ABSENT,
        )

    if os.name != "nt":
        assert (
            stat.S_IMODE(authority.parent.stat().st_mode)
            == PRIVATE_DIRECTORY_MODE
        )
        assert stat.S_IMODE(authority.stat().st_mode) == PRIVATE_FILE_MODE
    assert authority.read_bytes() == AUTHORITY_PAYLOAD


@pytest.mark.skipif(os.name == "nt", reason="POSIX ancestor permissions")
def test_dangling_and_writable_ancestors_are_unsafe(tmp_path: Path) -> None:
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "missing", target_is_directory=True)
    dangling_filesystem = PersistenceFilesystem(dangling / "accounts.json")
    with pytest.raises(UnsafeManagedFileError):
        dangling_filesystem.qualify()

    writable = tmp_path / "writable"
    writable.mkdir()
    writable.chmod(0o777)
    writable_filesystem = PersistenceFilesystem(
        writable / "sidekick" / "accounts.json"
    )
    with pytest.raises(UnsafeManagedFileError):
        writable_filesystem.qualify()


@pytest.mark.skipif(os.name == "nt", reason="POSIX preserved-name behavior")
def test_case_variant_and_insecure_mode_never_become_authority(
    tmp_path: Path,
) -> None:
    filesystem = PersistenceFilesystem(tmp_path / "state" / "accounts.json")
    filesystem._prepare_parent()
    variant = filesystem.authority_path.with_name("ACCOUNTS.JSON")
    variant.write_bytes(AUTHORITY_PAYLOAD)
    variant.chmod(0o600)

    with pytest.raises(UnsafeManagedFileError):
        filesystem.read_authority()
    assert variant.read_bytes() == AUTHORITY_PAYLOAD

    filesystem.authority_path.write_bytes(AUTHORITY_PAYLOAD)
    filesystem.authority_path.chmod(0o640)
    with pytest.raises(UnsafeManagedFileError):
        filesystem.read_authority()


def test_qualification_preserves_security_vs_capability_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = PersistenceFilesystem(tmp_path / "state" / "accounts.json")

    def fail_with(kind: NativeFailureKind) -> None:
        raise NativeFilesystemError(kind)

    monkeypatch.setattr(
        filesystem._native,
        "qualify",
        lambda _parent: fail_with(NativeFailureKind.UNSAFE),
    )
    with pytest.raises(UnsafeManagedFileError):
        filesystem.qualify()

    monkeypatch.setattr(
        filesystem._native,
        "qualify",
        lambda _parent: fail_with(NativeFailureKind.UNSUPPORTED),
    )
    with pytest.raises(UnsupportedFilesystemError):
        filesystem.qualify()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor injection")
def test_unknown_descriptor_failure_is_preserved_with_cleanup_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = os.open("/dev/null", os.O_RDONLY)
    original_close = os.close
    expected = KeyboardInterrupt()

    def fail_target_close(candidate: int) -> None:
        if candidate == descriptor:
            raise OSError("injected close failure")
        original_close(candidate)

    monkeypatch.setattr(os, "close", fail_target_close)
    try:
        with (
            pytest.raises(KeyboardInterrupt) as exc_info,
            owned_descriptor(
                descriptor,
                NativeFailureKind.UNREADABLE,
            ),
        ):
            raise expected
        assert exc_info.value is expected
        assert expected.__notes__ == ["Native descriptor cleanup also failed."]
    finally:
        original_close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor injection")
@pytest.mark.parametrize(
    "allow_interrupted_link",
    [False, True],
    ids=("single-link-candidate", "two-link-interrupted-publication"),
)
def test_posix_removal_rejects_pre_unlink_namespace_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    allow_interrupted_link: bool,
) -> None:
    basename = "candidate.tmp"
    survivor_basename = "survivor.tmp"
    replacement_basename = "replacement.tmp"
    candidate = tmp_path / basename
    survivor = tmp_path / survivor_basename
    replacement = tmp_path / replacement_basename
    secret = b"test-only-secret"
    candidate.write_bytes(secret)
    candidate.chmod(PRIVATE_FILE_MODE)
    replacement.write_bytes(b"test-only-replacement")
    replacement.chmod(PRIVATE_FILE_MODE)
    if allow_interrupted_link:
        os.link(candidate, tmp_path / "published.json")
    identity = candidate.stat()
    original_unlink = os.unlink

    def replace_immediately_before_unlink(
        path: str | bytes | Path,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if path == basename and dir_fd is not None:
            os.rename(
                basename,
                survivor_basename,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
            os.rename(
                replacement_basename,
                basename,
                src_dir_fd=dir_fd,
                dst_dir_fd=dir_fd,
            )
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", replace_immediately_before_unlink)
    platform = PosixPlatform()

    def remove() -> bool:
        if allow_interrupted_link:
            return platform.remove_validated(
                tmp_path,
                basename,
                identity.st_dev,
                identity.st_ino,
            )
        return platform.remove_candidate(tmp_path, basename)

    with pytest.raises(NativeFilesystemError) as exc_info:
        remove()

    assert exc_info.value.kind is NativeFailureKind.CHANGED
    assert survivor.read_bytes() == secret
    assert not candidate.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="macOS fcntl contract")
def test_macos_requires_apfs_and_issues_full_file_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported = {"filesystem": "apfs"}

    monkeypatch.setattr(
        adapter,
        "_filesystem_name",
        lambda _descriptor: reported["filesystem"],
    )
    platform = MacOSPlatform()
    assert platform.qualify(tmp_path) is FilesystemFamily.APFS

    reported["filesystem"] = "ext4"
    with pytest.raises(NativeFilesystemError) as exc_info:
        platform.qualify(tmp_path)
    assert exc_info.value.kind is NativeFailureKind.UNSUPPORTED

    calls: list[tuple[str, int, int | None]] = []

    def synchronize(descriptor: int) -> None:
        calls.append(("fsync", descriptor, None))

    def full_synchronize(descriptor: int, operation: int) -> int:
        calls.append(("F_FULLFSYNC", descriptor, operation))
        return 0

    monkeypatch.setattr(os, "fsync", synchronize)
    monkeypatch.setattr(fcntl, "F_FULLFSYNC", 51, raising=False)
    monkeypatch.setattr(fcntl, "fcntl", full_synchronize)
    platform._synchronize_file(7)

    assert calls == [("fsync", 7, None), ("F_FULLFSYNC", 7, 51)]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS APFS gate")
def test_macos_real_descriptor_reports_apfs(tmp_path: Path) -> None:
    assert MacOSPlatform().qualify(tmp_path) is FilesystemFamily.APFS


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DACL policy")
def test_windows_dacl_rejects_missing_inherited_and_foreign_access() -> None:
    allowed = _allowed_sids()

    valid = win32security.ACL()
    for sid in allowed:
        valid.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            win32file.FILE_ALL_ACCESS,
            sid,
        )
    _validate_acl(valid, directory=False)

    missing_principal = win32security.ACL()
    for sid in allowed[:-1]:
        missing_principal.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            win32file.FILE_ALL_ACCESS,
            sid,
        )

    inherited = win32security.ACL()
    for sid in allowed:
        inherited.AddAccessAllowedAceEx(
            win32security.ACL_REVISION,
            win32security.INHERITED_ACE,
            win32file.FILE_ALL_ACCESS,
            sid,
        )

    foreign = win32security.ACL()
    for sid in allowed:
        foreign.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            win32file.FILE_ALL_ACCESS,
            sid,
        )
    foreign.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32file.FILE_ALL_ACCESS,
        win32security.CreateWellKnownSid(win32security.WinWorldSid),
    )

    partial = win32security.ACL()
    for index, sid in enumerate(allowed):
        partial.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            (
                win32file.FILE_GENERIC_READ
                if index == 0
                else win32file.FILE_ALL_ACCESS
            ),
            sid,
        )

    missing_directory_inheritance = win32security.ACL()
    for sid in allowed:
        missing_directory_inheritance.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            win32file.FILE_ALL_ACCESS,
            sid,
        )

    for candidate in (missing_principal, inherited, foreign):
        with pytest.raises(NativeFilesystemError) as exc_info:
            _validate_acl(candidate, directory=False)
        assert exc_info.value.kind is NativeFailureKind.UNSAFE

    for candidate, directory in (
        (partial, False),
        (missing_directory_inheritance, True),
    ):
        with pytest.raises(NativeFilesystemError) as exc_info:
            _validate_acl(candidate, directory=directory)
        assert exc_info.value.kind is NativeFailureKind.UNSAFE


@pytest.mark.skipif(sys.platform != "win32", reason="Windows external ACL")
def test_windows_external_source_acl_separates_parent_and_file_access() -> (
    None
):
    trusted = _allowed_sids()
    world = win32security.CreateWellKnownSid(win32security.WinWorldSid)
    inherited_read = win32security.ACL()
    inherited_read.AddAccessAllowedAceEx(
        win32security.ACL_REVISION,
        win32security.INHERITED_ACE,
        win32file.FILE_GENERIC_READ,
        world,
    )
    effective_read = win32security.ACL()
    effective_read.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32file.FILE_GENERIC_READ,
        world,
    )
    effective_write = win32security.ACL()
    effective_write.AddAccessAllowedAce(
        win32security.ACL_REVISION,
        win32file.FILE_GENERIC_WRITE,
        world,
    )
    unknown_inherit_only = win32security.ACL()
    unknown_inherit_only.AddMandatoryAce(
        win32security.ACL_REVISION,
        win32security.INHERIT_ONLY_ACE,
        win32security.SYSTEM_MANDATORY_LABEL_NO_WRITE_UP,
        win32security.CreateWellKnownSid(win32security.WinLowLabelSid),
    )

    _validate_external_acl(
        inherited_read,
        trusted,
        ntsecuritycon.FILE_WRITE_DATA,
    )
    with pytest.raises(NativeFilesystemError):
        _validate_external_acl(
            effective_write,
            trusted,
            ntsecuritycon.FILE_WRITE_DATA,
        )
    with pytest.raises(NativeFilesystemError):
        _validate_external_acl(
            effective_read,
            trusted,
            ntsecuritycon.FILE_READ_DATA | ntsecuritycon.FILE_WRITE_DATA,
        )
    with pytest.raises(NativeFilesystemError):
        _validate_external_acl(
            unknown_inherit_only,
            trusted,
            ntsecuritycon.FILE_READ_DATA | ntsecuritycon.FILE_WRITE_DATA,
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DACL repair")
def test_windows_repairs_caller_owned_inherited_directory(
    tmp_path: Path,
) -> None:
    filesystem = PersistenceFilesystem(tmp_path / "accounts.json")

    assert filesystem.repair_parent_permissions()
    assert not filesystem.repair_parent_permissions()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows namespace and volume policy",
)
def test_windows_requires_non_reparse_fixed_ntfs_with_persistent_acls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        namespace,
        "path_attributes",
        lambda _path: (
            stat.FILE_ATTRIBUTE_DIRECTORY | stat.FILE_ATTRIBUTE_REPARSE_POINT
        ),
    )
    with pytest.raises(NativeFilesystemError) as exc_info:
        existing_ancestor(Path("C:/state"))
    assert exc_info.value.kind is NativeFailureKind.UNSAFE

    monkeypatch.setattr(
        win32file,
        "GetVolumePathName",
        lambda _path: "C:\\",
    )
    monkeypatch.setattr(
        win32file,
        "GetDriveTypeW",
        lambda _path: win32file.DRIVE_FIXED,
    )
    volume = {
        "flags": win32con.FILE_PERSISTENT_ACLS,
        "filesystem": "NTFS",
    }
    monkeypatch.setattr(
        win32api,
        "GetVolumeInformation",
        lambda _path: (
            "",
            1,
            255,
            volume["flags"],
            volume["filesystem"],
        ),
    )
    assert qualify_local_ntfs(Path("C:/state")) is FilesystemFamily.NTFS

    for flags, filesystem in (
        (0, "NTFS"),
        (win32con.FILE_PERSISTENT_ACLS, "ReFS"),
    ):
        volume.update(flags=flags, filesystem=filesystem)
        with pytest.raises(NativeFilesystemError) as exc_info:
            qualify_local_ntfs(Path("C:/state"))
        assert exc_info.value.kind is NativeFailureKind.UNSUPPORTED

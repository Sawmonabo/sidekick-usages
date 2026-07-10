"""Windows namespace identity and local-NTFS qualification."""

import ntpath
import stat
import sys
from pathlib import Path

from sidekick_usages.persistence._platform import (
    FilesystemFamily,
    NativeFailureKind,
    NativeFilesystemError,
)

if sys.platform == "win32":
    import msvcrt

    import pywintypes
    import win32api
    import win32con
    import win32file
    import winerror

    _NOT_FOUND_ERRORS = {
        winerror.ERROR_FILE_NOT_FOUND,
        winerror.ERROR_PATH_NOT_FOUND,
    }

    def _native_error(kind: NativeFailureKind) -> NativeFilesystemError:
        return NativeFilesystemError(kind)

    def path_attributes(path: Path) -> int | None:
        """Return exact no-follow attributes or proven absence."""
        try:
            attributes = win32file.GetFileAttributesW(str(path))
        except pywintypes.error as error:
            if error.winerror in _NOT_FOUND_ERRORS:
                return None
            raise _native_error(NativeFailureKind.UNSAFE) from None
        if type(attributes) is not int:
            raise _native_error(NativeFailureKind.UNSAFE)
        return attributes

    def existing_ancestor(path: Path) -> Path:
        """Return the nearest exact non-reparse directory ancestor."""
        candidate = path
        while path_attributes(candidate) is None:
            parent = candidate.parent
            if parent == candidate:
                raise _native_error(NativeFailureKind.UNSUPPORTED)
            candidate = parent
        attributes = path_attributes(candidate)
        if attributes is None or (
            attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not attributes & stat.FILE_ATTRIBUTE_DIRECTORY
        ):
            raise _native_error(NativeFailureKind.UNSAFE)
        return candidate

    def _normalized_final_path(value: str) -> str:
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return ntpath.normpath(value)

    def _final_path(handle: int) -> str:
        try:
            return _normalized_final_path(
                win32file.GetFinalPathNameByHandle(handle, 0)
            )
        except pywintypes.error:
            raise _native_error(NativeFailureKind.UNSAFE) from None

    def validate_membership(
        parent_descriptor: int,
        child_handle: int,
        basename: str,
    ) -> None:
        """Prove a handle resolves to one exact preserved child name."""
        try:
            parent_handle = msvcrt.get_osfhandle(parent_descriptor)
        except OSError:
            raise _native_error(NativeFailureKind.UNSAFE) from None
        parent_path = _final_path(parent_handle)
        child_path = _final_path(child_handle)
        if (
            ntpath.normcase(ntpath.dirname(child_path))
            != ntpath.normcase(parent_path)
            or ntpath.basename(child_path) != basename
        ):
            raise _native_error(NativeFailureKind.UNSAFE)

    def _win32_name_key(value: str) -> str:
        return ntpath.normcase(value.rstrip(" ."))

    def require_exact_entry(parent: Path, basename: str) -> bool:
        """Reject aliases without opening their contents."""
        try:
            entries = tuple(entry.name for entry in parent.iterdir())
        except OSError:
            raise _native_error(NativeFailureKind.UNREADABLE) from None
        if basename in entries:
            return True
        requested = _win32_name_key(basename)
        if any(_win32_name_key(entry) == requested for entry in entries):
            raise _native_error(NativeFailureKind.UNSAFE)
        return False

    def child_path(parent: Path, basename: str) -> Path:
        """Return one safe lexical child path."""
        if (
            not basename
            or Path(basename).name != basename
            or "/" in basename
            or "\\" in basename
        ):
            raise _native_error(NativeFailureKind.UNSAFE)
        return parent / basename

    def qualify_local_ntfs(ancestor: Path) -> FilesystemFamily:
        """Require the actual ancestor volume to be fixed local NTFS."""
        text = str(ancestor)
        if text.startswith("\\\\") or not ancestor.anchor:
            raise _native_error(NativeFailureKind.UNSUPPORTED)
        try:
            volume_path = win32file.GetVolumePathName(text)
            drive_type = win32file.GetDriveTypeW(volume_path)
            _label, _serial, _maximum, flags, filesystem = (
                win32api.GetVolumeInformation(volume_path)
            )
        except pywintypes.error:
            raise _native_error(NativeFailureKind.UNSUPPORTED) from None
        volume = Path(volume_path)
        if (
            not volume.anchor
            or ntpath.normcase(volume_path) != ntpath.normcase(volume.anchor)
            or drive_type != win32file.DRIVE_FIXED
            or filesystem.casefold() != "ntfs"
            or not flags & win32con.FILE_PERSISTENT_ACLS
        ):
            raise _native_error(NativeFailureKind.UNSUPPORTED)
        return FilesystemFamily.NTFS


__all__ = [
    "child_path",
    "existing_ancestor",
    "path_attributes",
    "qualify_local_ntfs",
    "require_exact_entry",
    "validate_membership",
]

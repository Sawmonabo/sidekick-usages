"""Durable local Linux mount qualification."""

import os
import platform
import re
from pathlib import Path

from sidekick_usages.persistence.platform.contracts import (
    FilesystemFamily,
    NativeFailureKind,
    NativeFilesystemError,
)

_MOUNTINFO_PATH = Path("/proc/self/mountinfo")
_MOUNTINFO_LIMIT = 1024 * 1024
_MOUNT_ESCAPE = re.compile(r"\\(040|011|012|134)")
_MOUNT_ESCAPE_VALUES = {
    "040": " ",
    "011": "\t",
    "012": "\n",
    "134": "\\",
}
_ALLOWED_LINUX_FILESYSTEMS = {
    "ext4": FilesystemFamily.EXT4,
    "xfs": FilesystemFamily.XFS,
    "btrfs": FilesystemFamily.BTRFS,
}


def _unsupported() -> NativeFilesystemError:
    return NativeFilesystemError(NativeFailureKind.UNSUPPORTED)


def _decode_mount_path(value: str) -> Path:
    decoded = _MOUNT_ESCAPE.sub(
        lambda match: _MOUNT_ESCAPE_VALUES[match.group(1)],
        value,
    )
    return Path(decoded)


def _classify_linux_filesystem(name: str) -> FilesystemFamily:
    normalized = name.casefold()
    is_wsl = (
        "microsoft" in platform.release().casefold()
        or "WSL_INTEROP" in os.environ
    )
    if is_wsl and normalized != FilesystemFamily.EXT4:
        raise _unsupported()
    try:
        return _ALLOWED_LINUX_FILESYSTEMS[normalized]
    except KeyError:
        raise _unsupported() from None


def _mountinfo_bytes() -> bytes:
    try:
        with _MOUNTINFO_PATH.open("rb") as stream:
            payload = stream.read(_MOUNTINFO_LIMIT + 1)
    except OSError:
        raise _unsupported() from None
    if len(payload) > _MOUNTINFO_LIMIT:
        raise _unsupported()
    return payload


def filesystem_for_descriptor(descriptor: int) -> FilesystemFamily:
    """Classify the longest mount containing one open directory."""
    try:
        actual_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        text = _mountinfo_bytes().decode("utf-8")
    except OSError, UnicodeDecodeError:
        raise _unsupported() from None
    if not actual_path.is_absolute():
        raise _unsupported()

    selected: tuple[int, int, int, str] | None = None
    for line in text.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
            device_text = fields[2]
            mount_path = _decode_mount_path(fields[4])
            filesystem_name = fields[separator + 1]
            major_text, minor_text = device_text.split(":", 1)
            major = int(major_text)
            minor = int(minor_text)
        except IndexError, ValueError:
            raise _unsupported() from None
        if actual_path != mount_path and not actual_path.is_relative_to(
            mount_path
        ):
            continue
        candidate = (len(mount_path.parts), major, minor, filesystem_name)
        if selected is None or candidate[0] > selected[0]:
            selected = candidate
    if selected is None:
        raise _unsupported()

    try:
        metadata = os.fstat(descriptor)
    except OSError:
        raise _unsupported() from None
    _, major, minor, filesystem_name = selected
    if (
        os.major(metadata.st_dev) != major
        or os.minor(metadata.st_dev) != minor
    ):
        raise _unsupported()
    return _classify_linux_filesystem(filesystem_name)


__all__ = ["filesystem_for_descriptor"]

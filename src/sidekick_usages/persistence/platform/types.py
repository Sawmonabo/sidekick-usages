"""Closed native persistence values."""

from enum import StrEnum


class FilesystemFamily(StrEnum):
    """Qualified local filesystems supported by persistence."""

    EXT4 = "ext4"
    XFS = "xfs"
    BTRFS = "btrfs"
    APFS = "apfs"
    NTFS = "ntfs"


class NativeFailureKind(StrEnum):
    """Closed native failure categories."""

    UNSUPPORTED = "unsupported"
    UNSAFE = "unsafe"
    UNREADABLE = "unreadable"
    TOO_LARGE = "too_large"
    CHANGED = "changed"
    EXISTS = "exists"
    CREATE = "create"
    WRITE = "write"
    SYNCHRONIZE = "synchronize"
    PUBLISH = "publish"
    REPLACE = "replace"
    HARDEN = "harden"
    REMOVE = "remove"

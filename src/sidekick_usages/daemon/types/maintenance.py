"""Closed scheduled-maintenance operation types."""

from enum import StrEnum

__all__ = ["DaemonOperation"]


class DaemonOperation(StrEnum):
    """Supported scheduled-maintenance manager operations."""

    INSTALL = "install"
    STATUS = "status"
    UNINSTALL = "uninstall"

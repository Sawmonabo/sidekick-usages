"""Local control endpoint identity models."""

from dataclasses import dataclass

__all__ = ["SocketIdentity"]


@dataclass(frozen=True, slots=True)
class SocketIdentity:
    """Exact local control socket object created by the service."""

    device: int
    inode: int

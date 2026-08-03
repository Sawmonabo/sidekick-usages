"""Local control endpoint identity models."""

from dataclasses import dataclass

from sidekick_usages.daemon.models.protocol import ControlRequest
from sidekick_usages.platform.models import PeerIdentity


@dataclass(frozen=True, slots=True)
class SocketIdentity:
    """Exact local control socket object created by the service."""

    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class VerifiedControlRequest:
    """One strict request paired with immutable kernel peer evidence."""

    request: ControlRequest
    peer: PeerIdentity

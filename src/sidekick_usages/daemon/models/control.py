"""Local control endpoint identity models."""

import socket
from dataclasses import dataclass, field

from sidekick_usages.daemon.models.protocol import ControlRequest
from sidekick_usages.platform.models import PeerIdentity


@dataclass(frozen=True, slots=True)
class SocketIdentity:
    """Exact local control socket object created by the service."""

    device: int
    inode: int


@dataclass(slots=True)
class VerifiedControlRequest:
    """One strict request paired with immutable kernel peer evidence."""

    request: ControlRequest
    peer: PeerIdentity
    _protected_endpoint: socket.socket | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def take_protected_endpoint(self) -> socket.socket | None:
        """Transfer the received descriptor exactly once."""
        endpoint = self._protected_endpoint
        self._protected_endpoint = None
        return endpoint

    def close_protected_endpoint(self) -> None:
        """Close an attachment that was not transferred to selection."""
        endpoint = self._protected_endpoint
        self._protected_endpoint = None
        if endpoint is not None:
            endpoint.close()

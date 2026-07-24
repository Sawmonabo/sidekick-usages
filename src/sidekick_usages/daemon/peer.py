"""Operating-system proof for same-user local supervisor peers."""

import ctypes
import os
import socket
import struct
import sys
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

_LINUX_PEER_CREDENTIALS = struct.Struct("3i")


class PeerFailureCode(StrEnum):
    """Safe reasons an operating system could not prove a local peer."""

    DIFFERENT_USER = "different_user"
    FEATURE_DISABLED = "feature_disabled"
    PROOF_UNAVAILABLE = "proof_unavailable"


class PeerVerificationError(PermissionError):
    """The connected peer cannot be proven to be this effective user."""

    def __init__(self, code: PeerFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class PeerIdentity:
    """Verified operating-system identity for one local connection."""

    effective_user_id: int

    def __post_init__(self) -> None:
        """Require a nonnegative effective user identifier."""
        if (
            isinstance(self.effective_user_id, bool)
            or not isinstance(self.effective_user_id, int)
            or self.effective_user_id < 0
        ):
            raise ValueError("Peer effective user ID is invalid.")


class PeerSocket(Protocol):
    """Socket operations required for operating-system peer proof."""

    def fileno(self) -> int:
        """Return the live file descriptor."""

    def getsockopt(
        self,
        level: int,
        option: int,
        buffer_length: int,
        /,
    ) -> bytes:
        """Read one socket option."""


class PeerVerifier(Protocol):
    """Prove that a local connection belongs to the effective user."""

    def verify(self, connection: PeerSocket) -> PeerIdentity:
        """Return a verified identity or fail closed."""


class OperatingSystemPeerVerifier:
    """Use Linux or macOS peer credentials before request decoding."""

    def __init__(self, expected_user_id: int | None = None) -> None:
        self._expected_user_id = expected_user_id
        if expected_user_id is None and sys.platform != "win32":
            self._expected_user_id = os.geteuid()
        if (
            self._expected_user_id is not None
            and self._expected_user_id < 0
        ):
            raise ValueError("Expected effective user ID is invalid.")

    def verify(self, connection: PeerSocket) -> PeerIdentity:
        """Prove one Linux, WSL, or macOS peer is the current user."""
        if sys.platform.startswith("linux"):
            identity = _linux_peer_identity(connection)
        elif sys.platform == "darwin":
            identity = _macos_peer_identity(connection)
        elif sys.platform == "win32":
            raise PeerVerificationError(PeerFailureCode.FEATURE_DISABLED)
        else:
            raise PeerVerificationError(PeerFailureCode.PROOF_UNAVAILABLE)
        if (
            self._expected_user_id is None
            or identity.effective_user_id != self._expected_user_id
        ):
            raise PeerVerificationError(PeerFailureCode.DIFFERENT_USER)
        return identity


def _linux_peer_identity(connection: PeerSocket) -> PeerIdentity:
    try:
        credentials = connection.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            _LINUX_PEER_CREDENTIALS.size,
        )
    except OSError:
        raise PeerVerificationError(
            PeerFailureCode.PROOF_UNAVAILABLE
        ) from None
    if not isinstance(credentials, bytes):
        raise PeerVerificationError(PeerFailureCode.PROOF_UNAVAILABLE)
    try:
        _process_id, user_id, _group_id = _LINUX_PEER_CREDENTIALS.unpack(
            credentials
        )
    except struct.error:
        raise PeerVerificationError(
            PeerFailureCode.PROOF_UNAVAILABLE
        ) from None
    return PeerIdentity(user_id)


def _macos_peer_identity(connection: PeerSocket) -> PeerIdentity:
    effective_user_id = ctypes.c_uint()
    effective_group_id = ctypes.c_uint()
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        result = libc.getpeereid(
            connection.fileno(),
            ctypes.byref(effective_user_id),
            ctypes.byref(effective_group_id),
        )
    except AttributeError, OSError:
        raise PeerVerificationError(
            PeerFailureCode.PROOF_UNAVAILABLE
        ) from None
    if result != 0:
        raise PeerVerificationError(PeerFailureCode.PROOF_UNAVAILABLE)
    return PeerIdentity(effective_user_id.value)


__all__ = [
    "OperatingSystemPeerVerifier",
    "PeerFailureCode",
    "PeerIdentity",
    "PeerSocket",
    "PeerVerificationError",
    "PeerVerifier",
]

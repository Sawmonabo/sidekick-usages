"""Operating-system proof for same-user local supervisor peers."""

import ctypes
import os
import socket
import struct
import sys

from sidekick_usages.daemon.models.peer import PeerIdentity
from sidekick_usages.daemon.types.peer import PeerFailureCode
from sidekick_usages.daemon.types.ports import PeerSocket

__all__ = [
    "OperatingSystemPeerVerifier",
    "PeerVerificationError",
]

_LINUX_PEER_CREDENTIALS = struct.Struct("3i")


class PeerVerificationError(PermissionError):
    """The connected peer cannot be proven to be this effective user."""

    def __init__(self, code: PeerFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class OperatingSystemPeerVerifier:
    """Use Linux or macOS peer credentials before request decoding."""

    def __init__(self, expected_user_id: int | None = None) -> None:
        self._expected_user_id = expected_user_id
        if expected_user_id is None and sys.platform != "win32":
            self._expected_user_id = os.geteuid()
        if self._expected_user_id is not None and self._expected_user_id < 0:
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

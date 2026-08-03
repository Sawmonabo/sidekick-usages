"""Operating-system proof for same-user local peers."""

import ctypes
import errno
import os
import socket
import struct
import sys
from contextlib import suppress
from pathlib import Path

from sidekick_usages.platform.models import PeerIdentity, ProcessIdentity
from sidekick_usages.platform.types import (
    PeerFailureCode,
    PeerSocket,
    ProcessLiveness,
)

_LINUX_PEER_CREDENTIALS = struct.Struct("3i")
_MAX_PROCESS_STAT_BYTES = 4096
_MACOS_PROCESS_INFO = struct.Struct("=12I16s32s6Iqq")
_MACOS_PROCESS_INFO_FLAVOR = 3


class PeerVerificationError(PermissionError):
    """The connected peer cannot be proven to be this effective user."""

    def __init__(self, code: PeerFailureCode) -> None:
        self.code = code
        super().__init__(code.value)


class _MacosProcessLookupError(PeerVerificationError):
    """Internal macOS lookup failure with captured absence proof."""

    def __init__(self, *, missing: bool) -> None:
        self.missing = missing
        super().__init__(PeerFailureCode.PROOF_UNAVAILABLE)


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


class OperatingSystemProcessInspector:
    """Compare one exact PID and start identity without signaling it."""

    def inspect(self, identity: ProcessIdentity) -> ProcessLiveness:
        """Return exact liveness or unknown when inspection is ambiguous."""
        if sys.platform.startswith("linux"):
            reader = _linux_process_start
        elif sys.platform == "darwin":
            reader = _macos_process_start
        else:
            return ProcessLiveness.UNKNOWN
        try:
            current = reader(identity.process_id)
        except _MacosProcessLookupError as error:
            return (
                ProcessLiveness.DEAD
                if error.missing
                else ProcessLiveness.UNKNOWN
            )
        except PeerVerificationError:
            if sys.platform.startswith("linux"):
                try:
                    os.stat(Path("/proc") / str(identity.process_id))
                except FileNotFoundError:
                    return ProcessLiveness.DEAD
                except OSError:
                    return ProcessLiveness.UNKNOWN
            return ProcessLiveness.UNKNOWN
        return (
            ProcessLiveness.ALIVE
            if current == identity.start_identity
            else ProcessLiveness.DEAD
        )


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
        process_id, user_id, _group_id = _LINUX_PEER_CREDENTIALS.unpack(
            credentials
        )
    except struct.error:
        raise PeerVerificationError(
            PeerFailureCode.PROOF_UNAVAILABLE
        ) from None
    process_identity: ProcessIdentity | None = None
    with suppress(PeerVerificationError, ValueError):
        process_identity = ProcessIdentity(
            process_id,
            _linux_process_start(process_id),
        )
    return PeerIdentity(user_id, process_identity)


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
    process_identity: ProcessIdentity | None = None
    with suppress(PeerVerificationError, ValueError):
        process_id = _macos_peer_process_id(connection)
        process_identity = ProcessIdentity(
            process_id,
            _macos_process_start(process_id),
        )
    return PeerIdentity(effective_user_id.value, process_identity)


def _linux_process_start(process_id: int) -> int:
    """Read one bounded `/proc` start-tick identity."""
    path = Path("/proc") / str(process_id) / "stat"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            payload = os.read(descriptor, _MAX_PROCESS_STAT_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError:
        raise PeerVerificationError(
            PeerFailureCode.PROOF_UNAVAILABLE
        ) from None
    if not payload or len(payload) > _MAX_PROCESS_STAT_BYTES:
        raise PeerVerificationError(PeerFailureCode.PROOF_UNAVAILABLE)
    closing = payload.rfind(b") ")
    if closing < 1:
        raise PeerVerificationError(PeerFailureCode.PROOF_UNAVAILABLE)
    fields = payload[closing + 2 :].split()
    try:
        start_identity = int(fields[19])
    except IndexError, ValueError:
        raise PeerVerificationError(
            PeerFailureCode.PROOF_UNAVAILABLE
        ) from None
    if start_identity <= 0:
        raise PeerVerificationError(PeerFailureCode.PROOF_UNAVAILABLE)
    return start_identity


def _macos_peer_process_id(connection: PeerSocket) -> int:
    """Read the kernel-owned local peer PID socket option."""
    local_level = getattr(socket, "SOL_LOCAL", None)
    peer_option = getattr(socket, "LOCAL_PEERPID", None)
    if local_level is None or peer_option is None:
        raise PeerVerificationError(PeerFailureCode.PROOF_UNAVAILABLE)
    try:
        payload = connection.getsockopt(
            local_level,
            peer_option,
            struct.calcsize("i"),
        )
        process_id = struct.unpack("i", payload)[0]
    except OSError, struct.error, TypeError:
        raise PeerVerificationError(
            PeerFailureCode.PROOF_UNAVAILABLE
        ) from None
    if process_id <= 0:
        raise PeerVerificationError(PeerFailureCode.PROOF_UNAVAILABLE)
    return process_id


def _macos_process_start(process_id: int) -> int:
    """Read one `proc_pidinfo` start-time identity."""
    buffer = ctypes.create_string_buffer(_MACOS_PROCESS_INFO.size)
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        ctypes.set_errno(0)
        result = libproc.proc_pidinfo(
            process_id,
            _MACOS_PROCESS_INFO_FLAVOR,
            0,
            buffer,
            _MACOS_PROCESS_INFO.size,
        )
        captured_errno = ctypes.get_errno()
    except AttributeError, OSError:
        raise PeerVerificationError(
            PeerFailureCode.PROOF_UNAVAILABLE
        ) from None
    if result != _MACOS_PROCESS_INFO.size:
        raise _MacosProcessLookupError(
            missing=captured_errno in {errno.ENOENT, errno.ESRCH}
        )
    values = _MACOS_PROCESS_INFO.unpack(buffer.raw)
    seconds = values[-2]
    microseconds = values[-1]
    start_identity = seconds * 1_000_000 + microseconds
    if start_identity <= 0:
        raise PeerVerificationError(PeerFailureCode.PROOF_UNAVAILABLE)
    return start_identity

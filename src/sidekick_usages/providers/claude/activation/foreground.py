"""Read-only same-user foreground Claude process discovery."""

import ctypes
import errno
import os
from pathlib import Path

from sidekick_usages.providers.claude.activation.types import (
    ClaudeForegroundState,
    ClaudeRemoteControlState,
)
from sidekick_usages.providers.claude.environment import (
    claude_keychain_environment,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import ClaudeExecutable
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)

_LINUX_PROCESS_ROOT = Path("/proc")
_LINUX_PROCESS_BYTES = 64 * 1024
_LINUX_STATUS_FIELD_COUNT = 5
_LINUX_STAT_REQUIRED_FIELDS = 6
_MACOS_PROCESS_BYTES = 2 * 1024 * 1024
_MACOS_PROCESS_FIELD_COUNT = 4
_MACOS_PROCESS_PATH_BYTES = 4096
_MACOS_PROCESS_TIMEOUT_SECONDS = 2.0
_MACOS_PS_EXECUTABLE = Path("/bin/ps")
_MACOS_PS_ARGUMENTS = ("-axo", "pid=,uid=,pgid=,tpgid=")
_MACOS_PLATFORMS = frozenset(
    {
        ClaudeManagedPlatform.MACOS_ARM64_KEYCHAIN,
        ClaudeManagedPlatform.MACOS_X64_KEYCHAIN,
    }
)
_PROC_PID_PATH_ERROR = -1


def inspect_claude_remote_control() -> ClaudeRemoteControlState:
    """Return no special guard without structured capability proof."""
    return ClaudeRemoteControlState.PROOF_UNAVAILABLE


def inspect_claude_foreground(
    executable: ClaudeExecutable,
    platform: ClaudeManagedPlatform,
) -> ClaudeForegroundState:
    """Inspect exact same-user terminal foregrounds without signaling them."""
    if platform in {
        ClaudeManagedPlatform.LINUX_FILE,
        ClaudeManagedPlatform.WSL_FILE,
    }:
        return _inspect_linux_foreground(executable)
    if platform in _MACOS_PLATFORMS:
        return _inspect_macos_foreground(executable)
    return ClaudeForegroundState.PROOF_UNAVAILABLE


def _inspect_linux_foreground(
    executable: ClaudeExecutable,
) -> ClaudeForegroundState:
    try:
        effective_user_id = os.geteuid()
        for entry in _LINUX_PROCESS_ROOT.iterdir():
            if not entry.name.isdecimal():
                continue
            foreground = _inspect_linux_process(
                entry,
                effective_user_id,
                executable,
            )
            if foreground is ClaudeForegroundState.PRESENT:
                return foreground
            if foreground is ClaudeForegroundState.PROOF_UNAVAILABLE:
                return foreground
    except OSError:
        return ClaudeForegroundState.PROOF_UNAVAILABLE
    return ClaudeForegroundState.CLEAR


def _inspect_linux_process(
    process: Path,
    effective_user_id: int,
    executable: ClaudeExecutable,
) -> ClaudeForegroundState | None:
    try:
        candidate = (
            int(process.name) != os.getpid()
            and process.stat().st_uid == effective_user_id
            and _linux_effective_user_id(process) == effective_user_id
            and _linux_executable_matches(process, executable)
        )
        return (
            ClaudeForegroundState.PRESENT
            if candidate and _linux_process_foreground(process)
            else None
        )
    except FileNotFoundError, ProcessLookupError:
        return None
    except OSError, ValueError:
        return ClaudeForegroundState.PROOF_UNAVAILABLE


def _linux_effective_user_id(process: Path) -> int | None:
    payload = _read_bounded(process / "status", _LINUX_PROCESS_BYTES)
    for line in payload.splitlines():
        fields = line.split()
        if len(fields) == _LINUX_STATUS_FIELD_COUNT and fields[0] == b"Uid:":
            return int(fields[2])
    raise ValueError("Linux process user identity is unavailable.")


def _linux_executable_matches(
    process: Path,
    executable: ClaudeExecutable,
) -> bool:
    file_status = (process / "exe").stat()
    provenance = executable.provenance
    return (
        file_status.st_dev == provenance.device
        and file_status.st_ino == provenance.inode
    )


def _linux_process_foreground(process: Path) -> bool:
    payload = _read_bounded(process / "stat", _LINUX_PROCESS_BYTES)
    closing_parenthesis = payload.rfind(b")")
    if closing_parenthesis < 0:
        raise ValueError("Linux process status is malformed.")
    fields = payload[closing_parenthesis + 1 :].split()
    if len(fields) < _LINUX_STAT_REQUIRED_FIELDS:
        raise ValueError("Linux process status is incomplete.")
    state = fields[0]
    process_group = int(fields[2])
    terminal = int(fields[4])
    terminal_foreground_group = int(fields[5])
    return (
        state != b"Z"
        and terminal != 0
        and process_group > 0
        and process_group == terminal_foreground_group
    )


def _inspect_macos_foreground(
    executable: ClaudeExecutable,
) -> ClaudeForegroundState:
    process_ids = _macos_foreground_process_ids()
    if process_ids is None:
        return ClaudeForegroundState.PROOF_UNAVAILABLE
    for process_id in process_ids:
        state = _macos_executable_state(process_id, executable)
        if state is not ClaudeForegroundState.CLEAR:
            return state
    return ClaudeForegroundState.CLEAR


def _macos_foreground_process_ids() -> tuple[int, ...] | None:
    try:
        result = run_bounded_claude_command(
            (str(_MACOS_PS_EXECUTABLE), *_MACOS_PS_ARGUMENTS),
            timeout_seconds=_MACOS_PROCESS_TIMEOUT_SECONDS,
            maximum_output_bytes=_MACOS_PROCESS_BYTES,
            environment=claude_keychain_environment({}),
        )
    except ClaudeProcessError:
        return None
    if result.return_code != 0:
        return None
    effective_user_id = os.geteuid()
    process_ids: list[int] = []
    for line in result.output.splitlines():
        fields = line.split()
        if len(fields) != _MACOS_PROCESS_FIELD_COUNT:
            return None
        try:
            process_id, user_id, process_group, foreground_group = (
                int(field) for field in fields
            )
        except ValueError:
            return None
        if (
            user_id == effective_user_id
            and process_group > 0
            and process_group == foreground_group
        ):
            process_ids.append(process_id)
    return tuple(process_ids)


def _macos_executable_state(
    process_id: int,
    executable: ClaudeExecutable,
) -> ClaudeForegroundState:
    candidate = _macos_process_path(process_id)
    if candidate is None:
        return (
            ClaudeForegroundState.CLEAR
            if ctypes.get_errno() == errno.ESRCH
            else ClaudeForegroundState.PROOF_UNAVAILABLE
        )
    try:
        file_status = candidate.stat()
    except FileNotFoundError:
        return ClaudeForegroundState.CLEAR
    except OSError:
        return ClaudeForegroundState.PROOF_UNAVAILABLE
    provenance = executable.provenance
    if (
        file_status.st_dev == provenance.device
        and file_status.st_ino == provenance.inode
    ):
        return ClaudeForegroundState.PRESENT
    return ClaudeForegroundState.CLEAR


def _macos_process_path(process_id: int) -> Path | None:
    try:
        library = ctypes.CDLL(
            "/usr/lib/libproc.dylib",
            use_errno=True,
        )
        process_path = library.proc_pidpath
    except AttributeError, OSError:
        ctypes.set_errno(_PROC_PID_PATH_ERROR)
        return None
    process_path.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    process_path.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(_MACOS_PROCESS_PATH_BYTES)
    length = process_path(
        process_id,
        buffer,
        _MACOS_PROCESS_PATH_BYTES,
    )
    if length <= 0:
        return None
    try:
        return Path(os.fsdecode(buffer.value))
    except UnicodeDecodeError:
        ctypes.set_errno(_PROC_PID_PATH_ERROR)
        return None


def _read_bounded(path: Path, maximum_bytes: int) -> bytes:
    with path.open("rb", buffering=0) as source:
        payload = source.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise ValueError("Process metadata exceeded its safe bound.")
    return payload

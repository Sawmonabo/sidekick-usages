"""Installed-console cached first-paint measurement on a Unix PTY."""

import errno
import fcntl
import os
import re
import selectors
import struct
import subprocess
import termios
import time
from collections.abc import Mapping
from pathlib import Path

from dashboard_benchmark.environment import (
    ALLOWED_PROVIDER_ENVIRONMENT_VARIABLES,
    ISOLATED_CONSOLE_PATHS,
    PROVIDER_ENVIRONMENT_PREFIXES,
)
from dashboard_benchmark.errors import DashboardBenchmarkError
from dashboard_benchmark.fixtures import (
    REFERENCE_ACCOUNT_COUNT,
    saved_accounts,
)
from sidekick_usages.branding.content import BRAND_TITLE
from sidekick_usages.cli.runtime.bootstrap import (
    PROCESS_LAUNCH_FAILURE_MESSAGE,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.platform.process import (
    SubprocessProcessGroup,
    terminate_process_group,
)
from sidekick_usages.usage.presentation.dashboard.render.text import (
    KEY_FOOTER,
)

COMPLETED_FRAME_REWIND = re.compile(rb"\x1b\[\d+D\x1b\[\d+A")
MAXIMUM_FRAME_BYTES = 1_048_576
MILLISECONDS_PER_SECOND = 1_000
FIRST_PAINT_DEADLINE_MILLISECONDS = 250
FIRST_PAINT_DIAGNOSTIC_TIMEOUT_MILLISECONDS = 1_000
FIRST_PAINT_DEADLINE_SECONDS = (
    FIRST_PAINT_DEADLINE_MILLISECONDS / MILLISECONDS_PER_SECOND
)
FIRST_PAINT_DIAGNOSTIC_TIMEOUT_SECONDS = (
    FIRST_PAINT_DIAGNOSTIC_TIMEOUT_MILLISECONDS / MILLISECONDS_PER_SECOND
)
NATURAL_EXIT_TIMEOUT_SECONDS = 5.0
PROCESS_GROUP_CLEANUP_SECONDS = 1.0
PTY_COLUMNS = 120
PTY_ROWS = 48
READ_BYTES = 65_536
PROVIDER_COMMAND_NAMES = ("claude", "codex")
FORBIDDEN_FRAME_FRAGMENTS = (
    b"error",
    b"failed",
    b"traceback",
    PROCESS_LAUNCH_FAILURE_MESSAGE.casefold().encode(),
)


def _isolated_paths(paths: ApplicationPaths) -> tuple[Path, ...]:
    return (
        paths.accounts,
        paths.private_credentials,
        paths.private_codex_profiles,
        paths.activity_snapshots,
        paths.usage_snapshots,
        paths.credential_refresh,
        paths.private_claude_profiles,
        paths.selected_state,
        paths.activation_journals,
        paths.durable_operations,
        paths.service_state,
        paths.service_setup_acknowledgement,
        paths.service_logs,
        paths.runtime_directory,
        paths.supervisor_socket,
        paths.systemd_user_service,
        paths.launch_agent,
    )


def _isolated_home(
    environment: Mapping[str, str],
) -> Path:
    home_value = environment.get("HOME")
    if home_value is None:
        raise DashboardBenchmarkError(
            "Installed-console benchmark HOME is unavailable."
        )
    try:
        home = Path(home_value).resolve(strict=True)
    except OSError:
        raise DashboardBenchmarkError(
            "Installed-console benchmark HOME is unavailable."
        ) from None
    if not home.is_dir():
        raise DashboardBenchmarkError(
            "Installed-console benchmark HOME is not a directory."
        )
    return home


def require_isolated_console_environment(
    console_script: Path,
    paths: ApplicationPaths,
    environment: Mapping[str, str],
) -> Path:
    """Require all native and provider paths to stay below a synthetic home."""
    home = _isolated_home(environment)
    if environment.get("PATH") != str(console_script.parent):
        raise DashboardBenchmarkError(
            "Installed-console benchmark inherited a provider search path."
        )
    if any(
        console_script.with_name(name).exists()
        for name in PROVIDER_COMMAND_NAMES
    ):
        raise DashboardBenchmarkError(
            "Installed-console benchmark can resolve a provider command."
        )
    if any(
        name.startswith(PROVIDER_ENVIRONMENT_PREFIXES)
        and name not in ALLOWED_PROVIDER_ENVIRONMENT_VARIABLES
        for name in environment
    ):
        raise DashboardBenchmarkError(
            "Installed-console benchmark inherited a provider override."
        )
    configured_paths: list[Path] = []
    for name, _relative in ISOLATED_CONSOLE_PATHS:
        value = environment.get(name)
        if value is None:
            raise DashboardBenchmarkError(
                f"Installed-console benchmark {name} is unavailable."
            )
        configured_paths.append(Path(value))
    candidates = (*_isolated_paths(paths), *configured_paths)
    try:
        isolated = all(
            candidate.is_absolute()
            and candidate.resolve(strict=False).is_relative_to(home)
            for candidate in candidates
        )
    except OSError, RuntimeError:
        isolated = False
    if not isolated:
        raise DashboardBenchmarkError(
            "Installed-console benchmark resolved a live application or "
            "provider path."
        )
    return home


def _set_terminal_size(file_descriptor: int) -> None:
    size = struct.pack("HHHH", PTY_ROWS, PTY_COLUMNS, 0, 0)
    fcntl.ioctl(file_descriptor, termios.TIOCSWINSZ, size)


def _expected_frame_fragments() -> tuple[bytes, ...]:
    accounts = saved_accounts(REFERENCE_ACCOUNT_COUNT)
    provider_sections: list[bytes] = []
    for provider_id in ProviderId:
        account_count = sum(
            account.provider_id is provider_id for account in accounts
        )
        provider_sections.append(
            (
                f"{provider_id.value.upper()} · {account_count} accounts"
            ).encode()
        )
    labels = tuple(str(account.label).encode() for account in accounts)
    return (
        BRAND_TITLE.encode(),
        *provider_sections,
        *labels,
        KEY_FOOTER.encode(),
    )


def _validate_completed_frame(frame: bytes) -> None:
    if any(fragment not in frame for fragment in _expected_frame_fragments()):
        raise DashboardBenchmarkError(
            "Installed console emitted an incomplete cached frame."
        )
    folded = frame.lower()
    if any(fragment in folded for fragment in FORBIDDEN_FRAME_FRAGMENTS):
        raise DashboardBenchmarkError(
            "Installed console emitted failure text before cached first paint."
        )


def _read_frame_chunk(
    process: subprocess.Popen[bytes],
    master_descriptor: int,
) -> bytes | None:
    try:
        chunk = os.read(master_descriptor, READ_BYTES)
    except BlockingIOError:
        return None
    except OSError as error:
        if error.errno != errno.EIO:
            raise DashboardBenchmarkError(
                "Installed-console pseudoterminal read failed."
            ) from error
        chunk = b""
    if chunk:
        return chunk
    status = process.poll()
    detail = (
        "without an exit status" if status is None else f"with status {status}"
    )
    raise DashboardBenchmarkError(
        f"Installed console exited before a completed cached frame {detail}."
    )


def _completed_frame(
    process: subprocess.Popen[bytes],
    master_descriptor: int,
    started_at: float,
) -> float:
    output = bytearray()
    match: re.Match[bytes] | None = None
    completed_at: float | None = None
    selector = selectors.DefaultSelector()
    selector.register(master_descriptor, selectors.EVENT_READ)
    try:
        while match is None:
            elapsed = time.perf_counter() - started_at
            diagnostic_remaining = (
                FIRST_PAINT_DIAGNOSTIC_TIMEOUT_SECONDS - elapsed
            )
            if diagnostic_remaining <= 0:
                raise DashboardBenchmarkError(
                    "Installed-console cached first paint exceeded the "
                    f"{FIRST_PAINT_DEADLINE_MILLISECONDS} ms deadline; no "
                    "completed frame arrived within "
                    f"{FIRST_PAINT_DIAGNOSTIC_TIMEOUT_MILLISECONDS} ms."
                )
            remaining = min(
                diagnostic_remaining,
                max(0.0, FIRST_PAINT_DEADLINE_SECONDS - elapsed),
            )
            if remaining == 0:
                remaining = diagnostic_remaining
            if not selector.select(remaining):
                continue
            chunk = _read_frame_chunk(process, master_descriptor)
            if chunk is None:
                continue
            output.extend(chunk)
            if len(output) > MAXIMUM_FRAME_BYTES:
                raise DashboardBenchmarkError(
                    "Installed-console cached frame exceeded its output bound."
                )
            match = COMPLETED_FRAME_REWIND.search(output)
            if match is not None:
                completed_at = time.perf_counter()
    finally:
        selector.close()
    if match is None or completed_at is None:
        raise DashboardBenchmarkError(
            "Installed-console completed-frame marker disappeared."
        )
    _validate_completed_frame(bytes(output[: match.end()]))
    elapsed = completed_at - started_at
    if elapsed > FIRST_PAINT_DEADLINE_SECONDS:
        raise DashboardBenchmarkError(
            "Installed-console cached first paint exceeded the "
            f"{FIRST_PAINT_DEADLINE_MILLISECONDS} ms deadline: observed "
            f"{elapsed * MILLISECONDS_PER_SECOND:.3f} ms."
        )
    return elapsed * MILLISECONDS_PER_SECOND


def measure_installed_console_first_paint(
    console_script: Path,
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> float:
    """Measure first paint and require one naturally restored quit."""
    master_descriptor, slave_descriptor = os.openpty()
    process: subprocess.Popen[bytes] | None = None
    completed_naturally = False
    cleanup_succeeded = True
    try:
        _set_terminal_size(slave_descriptor)
        initial_terminal_attributes = termios.tcgetattr(slave_descriptor)
        os.set_blocking(master_descriptor, False)
        started_at = time.perf_counter()
        process = subprocess.Popen(
            [str(console_script)],
            cwd=cwd,
            env=dict(environment),
            stdin=slave_descriptor,
            stdout=slave_descriptor,
            stderr=slave_descriptor,
            close_fds=True,
            start_new_session=True,
        )
        first_paint_ms = _completed_frame(
            process,
            master_descriptor,
            started_at,
        )
        try:
            os.write(master_descriptor, b"q")
            status = process.wait(timeout=NATURAL_EXIT_TIMEOUT_SECONDS)
        except OSError as error:
            raise DashboardBenchmarkError(
                "Installed console did not accept its quit key."
            ) from error
        except subprocess.TimeoutExpired:
            raise DashboardBenchmarkError(
                "Installed console did not exit naturally after its quit key."
            ) from None
        if status != 0:
            raise DashboardBenchmarkError(
                "Installed console returned a nonzero status after its "
                f"quit key: {status}."
            )
        try:
            restored = (
                termios.tcgetattr(slave_descriptor)
                == initial_terminal_attributes
            )
        except OSError as error:
            raise DashboardBenchmarkError(
                "Installed-console terminal restoration could not be read."
            ) from error
        if not restored:
            raise DashboardBenchmarkError(
                "Installed console did not restore its terminal after quit."
            )
        if SubprocessProcessGroup(process).group_alive():
            raise DashboardBenchmarkError(
                "Installed-console process group survived its natural exit."
            )
        completed_naturally = True
        return first_paint_ms
    finally:
        try:
            if process is not None and not completed_naturally:
                cleanup_succeeded = (
                    terminate_process_group(
                        SubprocessProcessGroup(process),
                        PROCESS_GROUP_CLEANUP_SECONDS,
                    )
                    is not None
                )
        finally:
            os.close(slave_descriptor)
            os.close(master_descriptor)
        if not cleanup_succeeded:
            raise DashboardBenchmarkError(
                "Installed-console process group was not fully reaped."
            )

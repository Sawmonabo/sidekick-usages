"""Supported-shell resolution and reversible explicit enrollment."""

import difflib
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.cli.session.models import (
    ResolvedShellStartup,
    ShellEnrollmentState,
    ShellEnrollmentStatus,
    ShellIntegrationError,
    ShellIntegrationFailure,
    ShellIntegrationResult,
    ShellKind,
)
from sidekick_usages.persistence.shell import (
    ShellFileSnapshot,
    ShellFileStore,
    ShellPersistenceError,
)
from sidekick_usages.platform.models import ExecutableProvenance

_START_MARKER = "# >>> sidekick-usages session >>>"
_END_MARKER = "# <<< sidekick-usages session <<<"
_RESTORE_NO_NEWLINE_TAG = " # sidekick-usages:restore-no-final-newline"
_POSIX_ALIAS_RESET = "unalias claude codex 2>/dev/null || true"
_LEGACY_POSIX_FUNCTIONS = b"""claude() {
    command sidekick-usages session claude -- "$@"
}
codex() {
    command sidekick-usages session codex -- "$@"
}
"""
_LEGACY_FISH_FUNCTIONS = b"""function claude
    command sidekick-usages session claude -- $argv
end
function codex
    command sidekick-usages session codex -- $argv
end
"""
_PRECONDITIONS = (
    "absolute current-user paths",
    "regular non-symlink files",
    "stable bounded reads before compare-and-swap",
)


@dataclass(frozen=True, slots=True)
class _PlannedFile:
    path: Path
    root: Path
    before: ShellFileSnapshot | None
    after: bytes | None
    owner_only: bool


class ShellStartupResolver:
    """Resolve one supported interactive startup target without mutation."""

    def __init__(
        self,
        *,
        environment: Mapping[str, str],
        platform: str,
        posix_integration: Path,
        effective_user_id: int,
    ) -> None:
        self._environment = dict(environment)
        self._platform = platform
        self._posix_integration = posix_integration
        self.effective_user_id = effective_user_id

    def resolve(
        self,
        shell_kind: ShellKind | None,
    ) -> ResolvedShellStartup:
        """Return one canonical target or a typed ambiguity refusal."""
        if self._platform not in {"linux", "darwin"}:
            raise ShellIntegrationError(
                ShellIntegrationFailure.UNSUPPORTED,
                "Automatic shell enrollment is unsupported on this platform.",
            )
        home = self._absolute_environment_path("HOME", required=True)
        self._absolute_environment_path("ZDOTDIR", required=False)
        self._absolute_environment_path("XDG_CONFIG_HOME", required=False)
        if home is None or not self._posix_integration.is_absolute():
            raise ShellIntegrationError(
                ShellIntegrationFailure.UNSAFE_PATH,
                "Shell enrollment paths must be absolute.",
            )
        kind = shell_kind or self._detect(home)
        generated_root = self._root_for(self._posix_integration, home)
        if kind is ShellKind.BASH:
            return ResolvedShellStartup(
                kind=kind,
                startup_file=home / ".bashrc",
                generated_file=self._posix_integration,
                startup_root=home,
                generated_root=generated_root,
                requires_source_block=True,
            )
        if kind is ShellKind.ZSH:
            zdotdir = self._absolute_environment_path(
                "ZDOTDIR",
                required=False,
            )
            root = home if zdotdir is None else zdotdir
            return ResolvedShellStartup(
                kind=kind,
                startup_file=root / ".zshrc",
                generated_file=self._posix_integration,
                startup_root=root,
                generated_root=generated_root,
                requires_source_block=True,
            )
        config_root = self._absolute_environment_path(
            "XDG_CONFIG_HOME",
            required=False,
        )
        if config_root is None:
            config_root = home / ".config"
        fish_file = config_root / "fish" / "conf.d" / "sidekick-usages.fish"
        fish_root = self._root_for(fish_file, home)
        return ResolvedShellStartup(
            kind=kind,
            startup_file=fish_file,
            generated_file=fish_file,
            startup_root=fish_root,
            generated_root=fish_root,
            requires_source_block=False,
        )

    def _detect(self, home: Path) -> ShellKind:
        shell_name = Path(self._environment.get("SHELL", "")).name
        try:
            return ShellKind(shell_name)
        except ValueError:
            candidates = self._viable_candidates(home)
        if len(candidates) > 1:
            raise ShellIntegrationError(
                ShellIntegrationFailure.AMBIGUOUS,
                "Multiple supported shell startup files are viable; use "
                "--shell.",
            )
        if not candidates:
            raise ShellIntegrationError(
                ShellIntegrationFailure.UNSUPPORTED,
                "No supported interactive shell could be resolved.",
            )
        return candidates[0]

    def _viable_candidates(self, home: Path) -> tuple[ShellKind, ...]:
        zdotdir = self._absolute_environment_path(
            "ZDOTDIR",
            required=False,
        )
        config = self._absolute_environment_path(
            "XDG_CONFIG_HOME",
            required=False,
        )
        candidates = (
            (ShellKind.BASH, home / ".bashrc"),
            (ShellKind.ZSH, (home if zdotdir is None else zdotdir) / ".zshrc"),
            (
                ShellKind.FISH,
                (home / ".config" if config is None else config)
                / "fish"
                / "conf.d"
                / "sidekick-usages.fish",
            ),
        )
        return tuple(kind for kind, path in candidates if _lexists(path))

    def _absolute_environment_path(
        self,
        name: str,
        *,
        required: bool,
    ) -> Path | None:
        value = self._environment.get(name)
        if value is None or not value:
            if required:
                raise ShellIntegrationError(
                    ShellIntegrationFailure.UNSAFE_PATH,
                    f"{name} must identify an absolute directory.",
                )
            return None
        path = Path(value)
        if not path.is_absolute():
            raise ShellIntegrationError(
                ShellIntegrationFailure.UNSAFE_PATH,
                f"{name} must identify an absolute directory.",
            )
        return path

    @staticmethod
    def _root_for(path: Path, home: Path) -> Path:
        try:
            path.relative_to(home)
        except ValueError:
            return path.parent
        return home


class ShellEnrollment:
    """Install, inspect, and remove exact Sidekick shell content."""

    def __init__(
        self,
        resolver: ShellStartupResolver,
        sidekick_executable: ExecutableProvenance,
    ) -> None:
        self._resolver = resolver
        self._sidekick_executable = sidekick_executable

    def install(
        self,
        shell_kind: ShellKind | None,
        *,
        dry_run: bool,
    ) -> ShellIntegrationResult:
        """Install one idempotent marked shell forwarding integration."""
        resolved = self._resolver.resolve(shell_kind)
        changes = self._install_changes(resolved)
        return self._apply(changes, dry_run=dry_run)

    def uninstall(
        self,
        shell_kind: ShellKind | None,
        *,
        dry_run: bool,
    ) -> ShellIntegrationResult:
        """Remove only byte-matching Sidekick-owned shell content."""
        resolved = self._resolver.resolve(shell_kind)
        changes = self._uninstall_changes(resolved)
        return self._apply(changes, dry_run=dry_run)

    def status(
        self,
        shell_kind: ShellKind | None,
    ) -> ShellEnrollmentStatus:
        """Inspect enrollment without reading provider state."""
        try:
            resolved = self._resolver.resolve(shell_kind)
            changes = self._install_changes(resolved)
        except ShellIntegrationError as error:
            if error.code is ShellIntegrationFailure.AMBIGUOUS:
                state = ShellEnrollmentState.AMBIGUOUS
            elif error.code is ShellIntegrationFailure.UNSUPPORTED:
                state = ShellEnrollmentState.UNSUPPORTED
            else:
                state = ShellEnrollmentState.BYPASSED
            return ShellEnrollmentStatus(state, (), str(error))
        changed = any(
            (b"" if change.before is None else change.before.data)
            != (b"" if change.after is None else change.after)
            for change in changes
        )
        state = (
            ShellEnrollmentState.NOT_LOADED
            if changed
            else ShellEnrollmentState.INTEGRATED
        )
        return ShellEnrollmentStatus(
            state,
            _paths(changes),
            (
                "Shell forwarding is installed."
                if state is ShellEnrollmentState.INTEGRATED
                else "Shell forwarding is not installed."
            ),
        )

    def _install_changes(
        self,
        resolved: ResolvedShellStartup,
    ) -> tuple[_PlannedFile, ...]:
        functions = self._functions(resolved.kind)
        if not resolved.requires_source_block:
            current = self._read(
                resolved.startup_root,
                resolved.startup_file,
                owner_only=True,
            )
            if current is not None and not _managed_functions(
                current.data,
                resolved.kind,
            ):
                self._changed_file(resolved.startup_file, current.data)
            return (
                _PlannedFile(
                    resolved.startup_file,
                    resolved.startup_root,
                    current,
                    functions,
                    True,
                ),
            )
        startup = self._read(
            resolved.startup_root,
            resolved.startup_file,
            owner_only=False,
        )
        generated = self._read(
            resolved.generated_root,
            resolved.generated_file,
            owner_only=True,
        )
        if generated is not None and not _managed_functions(
            generated.data,
            resolved.kind,
        ):
            self._changed_file(resolved.generated_file, generated.data)
        source_before = b"" if startup is None else startup.data
        source_after = self._install_source_block(
            resolved.startup_file,
            source_before,
            resolved.generated_file,
        )
        return (
            _PlannedFile(
                resolved.startup_file,
                resolved.startup_root,
                startup,
                source_after,
                False,
            ),
            _PlannedFile(
                resolved.generated_file,
                resolved.generated_root,
                generated,
                functions,
                True,
            ),
        )

    def _uninstall_changes(
        self,
        resolved: ResolvedShellStartup,
    ) -> tuple[_PlannedFile, ...]:
        if not resolved.requires_source_block:
            current = self._read(
                resolved.startup_root,
                resolved.startup_file,
                owner_only=True,
            )
            if current is not None and not _managed_functions(
                current.data,
                resolved.kind,
            ):
                self._changed_file(resolved.startup_file, current.data)
            return (
                _PlannedFile(
                    resolved.startup_file,
                    resolved.startup_root,
                    current,
                    None,
                    True,
                ),
            )
        startup = self._read(
            resolved.startup_root,
            resolved.startup_file,
            owner_only=False,
        )
        generated = self._read(
            resolved.generated_root,
            resolved.generated_file,
            owner_only=True,
        )
        if generated is not None and not _managed_functions(
            generated.data,
            resolved.kind,
        ):
            self._changed_file(resolved.generated_file, generated.data)
        source_before = b"" if startup is None else startup.data
        source_after = self._remove_source_block(
            resolved.startup_file,
            source_before,
            resolved.generated_file,
        )
        return (
            _PlannedFile(
                resolved.startup_file,
                resolved.startup_root,
                startup,
                source_after,
                False,
            ),
            _PlannedFile(
                resolved.generated_file,
                resolved.generated_root,
                generated,
                None,
                True,
            ),
        )

    def _apply(
        self,
        changes: tuple[_PlannedFile, ...],
        *,
        dry_run: bool,
    ) -> ShellIntegrationResult:
        effective = tuple(
            change
            for change in changes
            if (b"" if change.before is None else change.before.data)
            != (b"" if change.after is None else change.after)
        )
        diffs = tuple(_diff(change) for change in effective)
        if not dry_run:
            ordered = sorted(
                effective,
                key=lambda change: (
                    change.owner_only
                    if change.after is None
                    else not change.owner_only
                ),
            )
            for change in ordered:
                store = self._store(change.root)
                try:
                    if change.after is None:
                        if change.before is not None:
                            store.remove(change.path, change.before)
                    else:
                        store.write(
                            change.path,
                            change.before,
                            change.after,
                            owner_only=change.owner_only,
                        )
                except ShellPersistenceError as error:
                    raise ShellIntegrationError(
                        ShellIntegrationFailure.FILESYSTEM,
                        "Shell files changed or failed owner qualification.",
                        path=change.path,
                    ) from error
        return ShellIntegrationResult(
            changed=bool(effective),
            dry_run=dry_run,
            paths=_paths(changes),
            preconditions=_PRECONDITIONS,
            diffs=diffs,
        )

    def _read(
        self,
        root: Path,
        path: Path,
        *,
        owner_only: bool,
    ) -> ShellFileSnapshot | None:
        try:
            return self._store(root).read(path, owner_only=owner_only)
        except ShellPersistenceError as error:
            raise ShellIntegrationError(
                ShellIntegrationFailure.UNSAFE_PATH,
                "Shell file is unsafe, unreadable, or not current-user owned.",
                path=path,
            ) from error

    def _store(self, root: Path) -> ShellFileStore:
        return ShellFileStore(root, self._resolver.effective_user_id)

    def _functions(self, shell_kind: ShellKind) -> bytes:
        executable = self._sidekick_executable.path
        if shell_kind is ShellKind.FISH:
            return _fish_functions(executable)
        return _posix_functions(executable)

    @staticmethod
    def _install_source_block(
        path: Path,
        payload: bytes,
        generated_file: Path,
    ) -> bytes:
        text = _decode_source(path, payload)
        location = _managed_range(path, text, generated_file)
        if location is not None:
            return payload
        prefix = text
        restore_no_newline = bool(prefix) and not prefix.endswith("\n")
        if restore_no_newline:
            prefix += "\n"
        return (
            prefix
            + _source_block(
                generated_file,
                restore_no_newline=restore_no_newline,
            )
        ).encode()

    @staticmethod
    def _remove_source_block(
        path: Path,
        payload: bytes,
        generated_file: Path,
    ) -> bytes:
        text = _decode_source(path, payload)
        location = _managed_range(path, text, generated_file)
        if location is None:
            return payload
        start, end, restore_no_newline = location
        lines = text.splitlines(keepends=True)
        before = "".join(lines[:start])
        if restore_no_newline:
            before = before.removesuffix("\n")
        return "".join((before, *lines[end:])).encode()

    @staticmethod
    def _changed_file(path: Path, payload: bytes) -> None:
        lines = max(1, len(payload.splitlines()))
        raise ShellIntegrationError(
            ShellIntegrationFailure.SOURCE_CHANGED,
            f"Managed shell content changed; remove {path} lines 1-{lines} "
            "manually after review.",
            path=path,
            manual_range=(1, lines),
        )


def _source_block(
    generated_file: Path,
    *,
    restore_no_newline: bool,
) -> str:
    source = f". {shlex.quote(str(generated_file))}"
    if restore_no_newline:
        source += _RESTORE_NO_NEWLINE_TAG
    return f"{_START_MARKER}\n{source}\n{_END_MARKER}\n"


def _posix_functions(sidekick_executable: Path) -> bytes:
    command = shlex.quote(str(sidekick_executable))
    return f"""{_POSIX_ALIAS_RESET}
claude() {{
    command {command} session claude -- "$@"
}}
codex() {{
    command {command} session codex -- "$@"
}}
""".encode()


def _fish_functions(sidekick_executable: Path) -> bytes:
    command = shlex.quote(str(sidekick_executable))
    return f"""function claude
    command {command} session claude -- $argv
end
function codex
    command {command} session codex -- $argv
end
""".encode()


def _managed_functions(payload: bytes, shell_kind: ShellKind) -> bool:
    legacy = (
        _LEGACY_FISH_FUNCTIONS
        if shell_kind is ShellKind.FISH
        else _LEGACY_POSIX_FUNCTIONS
    )
    if payload == legacy:
        return True
    try:
        lines = payload.decode("utf-8").splitlines()
        offset = int(lines[0] == _POSIX_ALIAS_RESET)
        commands = (
            tuple(shlex.split(lines[1 + offset].strip())),
            tuple(shlex.split(lines[4 + offset].strip())),
        )
        executable = Path(commands[0][1])
    except IndexError, UnicodeDecodeError, ValueError:
        return False
    argument = "$argv" if shell_kind is ShellKind.FISH else "$@"
    expected = (
        ("command", str(executable), "session", "claude", "--", argument),
        ("command", str(executable), "session", "codex", "--", argument),
    )
    if not executable.is_absolute() or commands != expected:
        return False
    rendered = (
        _fish_functions(executable)
        if shell_kind is ShellKind.FISH
        else _posix_functions(executable)
    )
    if payload == rendered:
        return True
    return (
        shell_kind is not ShellKind.FISH
        and payload == rendered.partition(b"\n")[2]
    )


def _managed_range(
    path: Path,
    text: str,
    generated_file: Path,
) -> tuple[int, int, bool] | None:
    lines = text.splitlines(keepends=True)
    starts = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n") == _START_MARKER
    ]
    ends = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n") == _END_MARKER
    ]
    if not starts and not ends:
        return None
    marker_lines = (*starts, *ends)
    structure_valid = (
        len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0]
    )
    if not structure_valid:
        if len(marker_lines) == 1:
            marker_line = marker_lines[0] + 1
            detail = f"remove {path} marker line {marker_line}"
            manual_range = (marker_line, marker_line)
        else:
            detail = (
                f"marker locations in {path} are ambiguous; inspect the "
                "actual marker lines"
            )
            manual_range = None
        raise ShellIntegrationError(
            ShellIntegrationFailure.SOURCE_CHANGED,
            f"Managed source block changed; {detail} manually after review.",
            path=path,
            manual_range=manual_range,
        )
    block = "".join(lines[starts[0] : ends[0] + 1])
    standard = _source_block(generated_file, restore_no_newline=False)
    restoring = _source_block(generated_file, restore_no_newline=True)
    restore_no_newline = block == restoring
    if block not in {standard, restoring} or (
        restore_no_newline and ends[0] + 1 != len(lines)
    ):
        manual_start = starts[0] + 1
        manual_end = ends[0] + 1
        raise ShellIntegrationError(
            ShellIntegrationFailure.SOURCE_CHANGED,
            f"Managed source block changed; remove {path} lines "
            f"{manual_start}-{manual_end} manually after review.",
            path=path,
            manual_range=(manual_start, manual_end),
        )
    return starts[0], ends[0] + 1, restore_no_newline


def _decode_source(path: Path, payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise ShellIntegrationError(
            ShellIntegrationFailure.UNSAFE_PATH,
            "Shell startup file is not valid UTF-8.",
            path=path,
        ) from None


def _diff(change: _PlannedFile) -> str:
    before = b"" if change.before is None else change.before.data
    after = b"" if change.after is None else change.after
    return "".join(
        difflib.unified_diff(
            before.decode("utf-8").splitlines(keepends=True),
            after.decode("utf-8").splitlines(keepends=True),
            fromfile=str(change.path),
            tofile=str(change.path),
        )
    )


def _paths(changes: tuple[_PlannedFile, ...]) -> tuple[Path, ...]:
    return tuple(dict.fromkeys(change.path for change in changes))


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True

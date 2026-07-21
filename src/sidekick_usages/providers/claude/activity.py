"""Read-only Claude Code token-activity collection."""

import os
from collections.abc import Iterator, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from stat import S_ISDIR, S_ISREG

from sidekick_usages.core.models import (
    TokenActivityReading,
    TokenActivitySummary,
    TokenActivityUnavailable,
)
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import ProviderId, TokenActivityScope
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.schemas import (
    ClaudeActivityCache,
    claude_failure,
    parse_activity_cache,
    parse_activity_record,
)
from sidekick_usages.serialization import decode_json_object

_STATS_CACHE_NAME = "stats-cache.json"
_PROJECTS_DIRECTORY = "projects"
_MAX_STATS_CACHE_BYTES = 16 * 1024 * 1024
_MAX_TRANSCRIPT_LINE_BYTES = 32 * 1024 * 1024
_MAX_ACTIVITY_FILES = 100_000
_MAX_TOKEN_COUNT = 9_223_372_036_854_775_807
_PROJECT_DEPTH = 1
_SESSION_DEPTH = 2
_SUBAGENT_DEPTH = 3


def discover_claude_config_dir(
    environment: Mapping[str, str] | None = None,
) -> Path:
    """Resolve Claude Code's provider-owned application-data directory."""
    source = os.environ if environment is None else environment
    configured = source.get("CLAUDE_CONFIG_DIR")
    if configured is None:
        return Path.home() / ".claude"
    if not configured:
        raise _source_error(
            ProviderFailureKind.MALFORMED,
            "Claude activity configuration is invalid.",
        )
    return Path(configured).expanduser().resolve(strict=False)


class ClaudeActivity:
    """Collect Claude Code's non-cached local-installation token history."""

    provider_id = ProviderId.CLAUDE

    def __init__(self, config_dir: Path) -> None:
        """Bind collection to one resolved provider-owned directory."""
        self._config_dir = config_dir

    def read(self, reference_time: datetime) -> TokenActivityReading:
        """Return Claude's historical cache plus its live UTC suffix."""
        today = as_utc(reference_time).date()
        cache = _load_cache(self._config_dir / _STATS_CACHE_NAME)
        if cache is not None and cache.last_computed_date > today:
            raise _source_error(
                ProviderFailureKind.MALFORMED,
                "Claude activity cache has a future computation date.",
            )

        files = _activity_files(self._config_dir / _PROJECTS_DIRECTORY)
        if cache is None and not files:
            return TokenActivityUnavailable(
                scope=TokenActivityScope.LOCAL_INSTALLATION
            )

        boundary = None if cache is None else cache.last_computed_date
        live_total, earliest_live = _collect_live(files, boundary, today)
        historical_total = 0 if cache is None else cache.total_tokens
        total = historical_total + live_total
        if total > _MAX_TOKEN_COUNT:
            raise _source_error(
                ProviderFailureKind.MALFORMED,
                "Claude activity total exceeds its boundary.",
            )

        if cache is None:
            since = earliest_live
        elif cache.first_session_date is not None:
            since = cache.first_session_date
        elif cache.total_tokens == 0:
            since = earliest_live
        else:
            since = None
        return TokenActivitySummary(
            total_tokens=total,
            scope=TokenActivityScope.LOCAL_INSTALLATION,
            since=since,
        )


def _source_error(
    kind: ProviderFailureKind,
    message: str,
) -> ProviderBoundaryError:
    return ProviderBoundaryError(
        claude_failure(
            kind,
            message,
            action_required=False,
        )
    )


def _load_cache(path: Path) -> ClaudeActivityCache | None:
    try:
        status = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        raise _source_error(
            ProviderFailureKind.UNREADABLE,
            "Claude activity cache is unreadable.",
        ) from None
    if not S_ISREG(status.st_mode):
        raise _source_error(
            ProviderFailureKind.UNREADABLE,
            "Claude activity cache is unreadable.",
        )
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not S_ISREG(opened.st_mode):
                raise OSError
            payload = stream.read(_MAX_STATS_CACHE_BYTES + 1)
    except OSError:
        raise _source_error(
            ProviderFailureKind.UNREADABLE,
            "Claude activity cache is unreadable.",
        ) from None
    if len(payload) > _MAX_STATS_CACHE_BYTES:
        raise _source_error(
            ProviderFailureKind.MALFORMED,
            "Claude activity cache exceeds its size boundary.",
        )
    try:
        return parse_activity_cache(decode_json_object(payload))
    except InvalidPayloadError:
        raise _source_error(
            ProviderFailureKind.MALFORMED,
            "Claude activity cache is malformed.",
        ) from None


type _ActivityFile = tuple[Path, bool]


def _activity_files(root: Path) -> tuple[_ActivityFile, ...]:
    try:
        status = root.stat(follow_symlinks=False)
    except FileNotFoundError:
        return ()
    except OSError:
        raise _source_error(
            ProviderFailureKind.UNREADABLE,
            "Claude activity transcripts are unreadable.",
        ) from None
    if not S_ISDIR(status.st_mode):
        raise _source_error(
            ProviderFailureKind.UNREADABLE,
            "Claude activity transcripts are unreadable.",
        )

    selected: list[_ActivityFile] = []
    try:
        for directory, directories, filenames in root.walk(
            top_down=True,
            on_error=_raise_walk_error,
            follow_symlinks=False,
        ):
            parts = directory.relative_to(root).parts
            selected.extend(
                _selected_files(
                    directory,
                    directories,
                    filenames,
                    parts,
                )
            )
            if len(selected) > _MAX_ACTIVITY_FILES:
                raise _source_error(
                    ProviderFailureKind.MALFORMED,
                    "Claude activity file count exceeds its boundary.",
                )
    except OSError:
        raise _source_error(
            ProviderFailureKind.UNREADABLE,
            "Claude activity transcripts are unreadable.",
        ) from None
    return tuple(sorted(selected, key=lambda item: str(item[0])))


def _selected_files(
    directory: Path,
    directories: list[str],
    filenames: list[str],
    parts: tuple[str, ...],
) -> tuple[_ActivityFile, ...]:
    depth = len(parts)
    if depth == _PROJECT_DEPTH:
        return tuple(
            (directory / filename, False)
            for filename in filenames
            if filename.endswith(".jsonl")
        )
    if depth == _SESSION_DEPTH:
        directories[:] = [name for name in directories if name == "subagents"]
        return ()
    if depth == _SUBAGENT_DEPTH and parts[-1] == "subagents":
        directories.clear()
        return tuple(
            (directory / filename, True)
            for filename in filenames
            if filename.startswith("agent-") and filename.endswith(".jsonl")
        )
    if depth >= _SUBAGENT_DEPTH:
        directories.clear()
    return ()


def _raise_walk_error(error: OSError) -> None:
    raise error


def _collect_live(
    files: tuple[_ActivityFile, ...],
    boundary: date | None,
    today: date,
) -> tuple[int, date | None]:
    total = 0
    earliest: date | None = None
    for path, is_subagent in files:
        file_total, file_earliest = _collect_file(
            path,
            is_subagent=is_subagent,
            boundary=boundary,
            today=today,
        )
        total += file_total
        if total > _MAX_TOKEN_COUNT:
            raise _source_error(
                ProviderFailureKind.MALFORMED,
                "Claude activity total exceeds its boundary.",
            )
        if file_earliest is not None and (
            earliest is None or file_earliest < earliest
        ):
            earliest = file_earliest
    return total, earliest


def _collect_file(
    path: Path,
    *,
    is_subagent: bool,
    boundary: date | None,
    today: date,
) -> tuple[int, date | None]:
    try:
        status = path.stat(follow_symlinks=False)
    except OSError:
        raise _source_error(
            ProviderFailureKind.UNREADABLE,
            "Claude activity transcript is unreadable.",
        ) from None
    if not S_ISREG(status.st_mode):
        raise _source_error(
            ProviderFailureKind.UNREADABLE,
            "Claude activity transcript is unreadable.",
        )
    if boundary is not None:
        modified = datetime.fromtimestamp(status.st_mtime, tz=UTC).date()
        if modified < boundary:
            return 0, None

    total = 0
    earliest: date | None = None
    try:
        for line in _snapshot_lines(path):
            included = _included_event(
                line,
                is_subagent=is_subagent,
                boundary=boundary,
                today=today,
            )
            if included is None:
                continue
            event_total, event_date = included
            total += event_total
            if total > _MAX_TOKEN_COUNT:
                raise _source_error(
                    ProviderFailureKind.MALFORMED,
                    "Claude activity total exceeds its boundary.",
                )
            if earliest is None or event_date < earliest:
                earliest = event_date
    except OSError:
        raise _source_error(
            ProviderFailureKind.UNREADABLE,
            "Claude activity transcript is unreadable.",
        ) from None
    return total, earliest


def _included_event(
    line: bytes,
    *,
    is_subagent: bool,
    boundary: date | None,
    today: date,
) -> tuple[int, date] | None:
    try:
        value = decode_json_object(line)
    except InvalidPayloadError:
        raise _source_error(
            ProviderFailureKind.MALFORMED,
            "Claude activity transcript is malformed.",
        ) from None
    event = parse_activity_record(value)
    if event is None or (event.is_sidechain and not is_subagent):
        return None
    event_date = event.occurred_at.date()
    if event_date > today or (boundary is not None and event_date < boundary):
        return None
    return event.total_tokens, event_date


def _snapshot_lines(path: Path) -> Iterator[bytes]:
    with path.open("rb") as stream:
        status = os.fstat(stream.fileno())
        if not S_ISREG(status.st_mode):
            raise OSError
        remaining = status.st_size
        while remaining:
            line = stream.readline(
                min(_MAX_TRANSCRIPT_LINE_BYTES + 1, remaining)
            )
            if not line:
                raise OSError
            remaining -= len(line)
            if len(line) > _MAX_TRANSCRIPT_LINE_BYTES:
                raise _source_error(
                    ProviderFailureKind.MALFORMED,
                    "Claude activity transcript record is too large.",
                )
            if not line.endswith(b"\n"):
                if remaining == 0:
                    return
                raise _source_error(
                    ProviderFailureKind.MALFORMED,
                    "Claude activity transcript record is too large.",
                )
            yield line


__all__ = ["ClaudeActivity", "discover_claude_config_dir"]

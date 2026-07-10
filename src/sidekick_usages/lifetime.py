"""Typed per-provider lifetime output-token collection.

Claude exposes a machine-wide aggregate while Codex exposes cumulative
session events. Collection keeps unavailable data, invalid input, and I/O
failures distinct from a valid zero-token total.
"""

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from stat import S_ISDIR
from typing import Never

from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.serialization import (
    JsonObject,
    JsonValue,
    decode_json_object,
)


class LifetimeFailureKind(StrEnum):
    """Closed vocabulary for lifetime collection failures."""

    SOURCE_UNREADABLE = "source_unreadable"
    SOURCE_MALFORMED = "source_malformed"
    CACHE_READ_FAILED = "cache_read_failed"
    CACHE_WRITE_FAILED = "cache_write_failed"


@dataclass(frozen=True, slots=True)
class LifetimeTotal:
    """A successfully collected provider lifetime total.

    :ivar output_tokens: Non-negative lifetime output-token count.
    :ivar since: Earliest source date, when the provider exposes one.
    """

    output_tokens: int
    since: date | None

    def __post_init__(self) -> None:
        """Reject values that cannot represent a token total."""
        if (
            isinstance(self.output_tokens, bool)
            or not isinstance(self.output_tokens, int)
            or self.output_tokens < 0
        ):
            raise ValueError(
                "Lifetime output tokens must be a non-negative integer."
            )


@dataclass(frozen=True, slots=True)
class LifetimeUnavailable:
    """The provider has no local lifetime source data."""


@dataclass(frozen=True, slots=True)
class LifetimeFailure:
    """Lifetime collection failed without manufacturing a total.

    :ivar kind: Stable failure category for policy and presentation.
    """

    kind: LifetimeFailureKind


type LifetimeResult = LifetimeTotal | LifetimeUnavailable | LifetimeFailure
type _LifetimeSource = Callable[[], LifetimeResult]
type _CodexCache = dict[str, tuple[int, int]]
type _CodexSources = tuple[list[Path], date]


class LifetimeCollector:
    """Collect configured provider lifetime totals without presentation."""

    def __init__(
        self,
        sources: Mapping[ProviderId, _LifetimeSource],
    ) -> None:
        """Own an exact copy of the configured provider sources."""
        self._sources = dict(sources)

    def collect(
        self,
        provider_ids: Iterable[ProviderId],
    ) -> dict[ProviderId, LifetimeResult]:
        """Collect configured sources selected by provider id."""
        selected = frozenset(provider_ids)
        return {
            provider_id: source()
            for provider_id, source in self._sources.items()
            if provider_id in selected
        }


#: Claude's machine-wide pre-aggregated stats (all Claude Code usage
#: on this machine, not just sidekick-managed accounts).
_CLAUDE_STATS_FILE = Path.home() / ".claude" / "stats-cache.json"

#: Codex session logs. This provider-native source remains Codex-owned.
_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"


def _source_date(value: JsonValue) -> date | None:
    """Parse an optional provider date at the source boundary."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError
    try:
        if "T" in value:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError from None


def _token_count(value: JsonValue) -> int:
    """Validate one non-negative output-token count."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def _claude_total(data: JsonObject) -> LifetimeTotal:
    """Validate and aggregate one decoded Claude stats object."""
    model_usage = data.get("modelUsage")
    if not isinstance(model_usage, dict):
        raise ValueError

    total = 0
    for usage in model_usage.values():
        if not isinstance(usage, dict) or "outputTokens" not in usage:
            raise ValueError
        total += _token_count(usage["outputTokens"])

    return LifetimeTotal(total, _source_date(data.get("firstSessionDate")))


def claude_lifetime_output() -> LifetimeResult:
    """Collect Claude lifetime output tokens from its local stats file.

    :returns: A valid total, unavailable source, or explicit failure.
    """
    try:
        payload = _CLAUDE_STATS_FILE.read_bytes()
    except FileNotFoundError:
        return LifetimeUnavailable()
    except OSError:
        return LifetimeFailure(LifetimeFailureKind.SOURCE_UNREADABLE)

    try:
        return _claude_total(decode_json_object(payload))
    except InvalidPayloadError, ValueError:
        return LifetimeFailure(LifetimeFailureKind.SOURCE_MALFORMED)


def _rollout_output(record: JsonObject) -> int | None:
    """Return cumulative output tokens from a token event, if present."""
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise ValueError
    info = payload.get("info")
    if info is None:
        return None
    if not isinstance(info, dict):
        raise ValueError
    if "total_token_usage" not in info:
        return None
    usage = info["total_token_usage"]
    if not isinstance(usage, dict) or "output_tokens" not in usage:
        raise ValueError
    return _token_count(usage["output_tokens"])


def _max_output_in_rollout(path: Path) -> int | LifetimeFailure:
    """Collect the maximum cumulative output count from one rollout."""
    best = 0
    try:
        with path.open("rb") as handle:
            for line in handle:
                try:
                    output = _rollout_output(decode_json_object(line))
                except InvalidPayloadError, ValueError:
                    return LifetimeFailure(
                        LifetimeFailureKind.SOURCE_MALFORMED
                    )
                if output is not None:
                    best = max(best, output)
    except OSError:
        return LifetimeFailure(LifetimeFailureKind.SOURCE_UNREADABLE)
    return best


def _rollout_date(path: Path) -> date | LifetimeFailure:
    """Parse the source date encoded in a Codex rollout filename."""
    prefix = "rollout-"
    raw_date = path.name.removeprefix(prefix)[: len("YYYY-MM-DD")]
    if not path.name.startswith(prefix):
        return LifetimeFailure(LifetimeFailureKind.SOURCE_MALFORMED)
    try:
        return date.fromisoformat(raw_date)
    except ValueError:
        return LifetimeFailure(LifetimeFailureKind.SOURCE_MALFORMED)


def _load_codex_cache(cache_file: Path) -> _CodexCache | LifetimeFailure:
    """Load and validate the optional Sidekick-owned Codex cache."""
    try:
        data = decode_json_object(cache_file.read_bytes())
    except FileNotFoundError:
        return {}
    except OSError, InvalidPayloadError:
        return LifetimeFailure(LifetimeFailureKind.CACHE_READ_FAILED)

    raw_entries = data.get("files")
    if set(data) != {"files"} or not isinstance(raw_entries, dict):
        return LifetimeFailure(LifetimeFailureKind.CACHE_READ_FAILED)

    if _is_released_legacy_cache(raw_entries):
        # Released caches used collision-prone basenames and float mtimes.
        # Their totals are never trusted; a complete source pass rebuilds
        # the cache in the current shape.
        return {}
    return _current_codex_cache(raw_entries)


def _is_valid_cache_key(value: str) -> bool:
    """Return whether a cache key is a canonical relative POSIX path."""
    key = PurePosixPath(value)
    return (
        value not in {"", "."}
        and not key.is_absolute()
        and ".." not in key.parts
        and key.as_posix() == value
        and "\\" not in value
    )


def _is_released_legacy_cache(entries: JsonObject) -> bool:
    """Recognize the exact released basename-plus-float cache shape."""
    if not entries:
        return False
    for filename, raw_entry in entries.items():
        if (
            not filename
            or PurePosixPath(filename).name != filename
            or not isinstance(raw_entry, dict)
            or set(raw_entry) != {"mtime", "output"}
        ):
            return False
        mtime = raw_entry.get("mtime")
        output = raw_entry.get("output")
        if (
            not isinstance(mtime, float)
            or mtime < 0
            or not isinstance(output, int)
            or isinstance(output, bool)
            or output < 0
        ):
            return False
    return True


def _current_codex_cache(entries: JsonObject) -> _CodexCache | LifetimeFailure:
    """Validate the current relative-path and nanosecond cache shape."""
    parsed: _CodexCache = {}
    for filename, raw_entry in entries.items():
        if (
            not _is_valid_cache_key(filename)
            or not isinstance(raw_entry, dict)
            or set(raw_entry) != {"mtime_ns", "output"}
        ):
            return LifetimeFailure(LifetimeFailureKind.CACHE_READ_FAILED)
        mtime_ns = raw_entry.get("mtime_ns")
        output = raw_entry.get("output")
        if (
            isinstance(mtime_ns, bool)
            or not isinstance(mtime_ns, int)
            or mtime_ns < 0
            or not isinstance(output, int)
            or isinstance(output, bool)
            or output < 0
        ):
            return LifetimeFailure(LifetimeFailureKind.CACHE_READ_FAILED)
        parsed[filename] = (mtime_ns, output)
    return parsed


def _save_codex_cache(
    cache_file: Path,
    entries: _CodexCache,
) -> LifetimeFailure | None:
    """Persist a fully collected cache or return its write failure."""
    payload = {
        "files": {
            filename: {"mtime_ns": mtime_ns, "output": output}
            for filename, (mtime_ns, output) in entries.items()
        }
    }
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps(payload, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        return LifetimeFailure(LifetimeFailureKind.CACHE_WRITE_FAILED)
    return None


def _raise_walk_error(error: OSError) -> Never:
    """Propagate a filesystem traversal error to the source boundary."""
    raise error


def _codex_sources() -> _CodexSources | LifetimeUnavailable | LifetimeFailure:
    """Find Codex rollouts and validate every filename date."""
    try:
        root_status = _CODEX_SESSIONS_DIR.stat()
    except FileNotFoundError:
        return LifetimeUnavailable()
    except OSError:
        return LifetimeFailure(LifetimeFailureKind.SOURCE_UNREADABLE)
    if not S_ISDIR(root_status.st_mode):
        return LifetimeFailure(LifetimeFailureKind.SOURCE_UNREADABLE)

    files: list[Path] = []
    try:
        for directory, _directories, filenames in _CODEX_SESSIONS_DIR.walk(
            on_error=_raise_walk_error
        ):
            files.extend(
                directory / filename
                for filename in filenames
                if filename.startswith("rollout-")
                and filename.endswith(".jsonl")
            )
    except OSError:
        return LifetimeFailure(LifetimeFailureKind.SOURCE_UNREADABLE)
    if not files:
        return LifetimeUnavailable()
    files.sort()

    return _dated_codex_sources(files)


def _dated_codex_sources(files: list[Path]) -> _CodexSources | LifetimeFailure:
    """Validate rollout filename dates and return their earliest date."""

    dates: list[date] = []
    for path in files:
        rollout_date = _rollout_date(path)
        if isinstance(rollout_date, LifetimeFailure):
            return rollout_date
        dates.append(rollout_date)
    return (files, min(dates))


def _collect_codex_rollouts(
    files: list[Path],
    entries: _CodexCache,
) -> tuple[int, bool] | LifetimeFailure:
    """Collect all rollouts into an in-memory cache candidate."""
    total = 0
    changed = False
    for path in files:
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return LifetimeFailure(LifetimeFailureKind.SOURCE_UNREADABLE)

        cache_key = path.relative_to(_CODEX_SESSIONS_DIR).as_posix()
        cached = entries.get(cache_key)
        if cached is not None and cached[0] == mtime_ns:
            output = cached[1]
        else:
            collected = _max_output_in_rollout(path)
            if isinstance(collected, LifetimeFailure):
                return collected
            output = collected
            entries[cache_key] = (mtime_ns, output)
            changed = True
        total += output
    return (total, changed)


def codex_lifetime_output(cache_file: Path) -> LifetimeResult:
    """Collect Codex lifetime output tokens across local rollout logs.

    Closed sessions are cached by filename and modification time. The cache
    is written only after every selected rollout has been collected.

    :param cache_file: Sidekick-owned incremental cache location.
    :returns: A valid total, unavailable source, or explicit failure.
    """
    sources = _codex_sources()
    if isinstance(sources, LifetimeUnavailable | LifetimeFailure):
        return sources
    files, since = sources

    loaded_cache = _load_codex_cache(cache_file)
    if isinstance(loaded_cache, LifetimeFailure):
        return loaded_cache
    entries = dict(loaded_cache)

    collected = _collect_codex_rollouts(files, entries)
    if isinstance(collected, LifetimeFailure):
        return collected
    total, changed = collected

    if changed and (failure := _save_codex_cache(cache_file, entries)):
        return failure
    return LifetimeTotal(total, since)

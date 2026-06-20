"""Per-provider lifetime OUTPUT-token aggregation (leak-free).

Reads local, read-only stats: Claude's pre-aggregated
``stats-cache.json`` and (in Task 5) the Codex rollout logs. Returns
``(output_total, since)`` per provider. Output tokens are the only
cross-provider-comparable measure (Claude reports cache-read
separately; Codex folds cached tokens into ``input_tokens``).
"""

import json
from datetime import datetime
from pathlib import Path
from typing import cast

#: Claude's machine-wide pre-aggregated stats (all Claude Code usage
#: on this machine, not just sidekick-managed accounts).
_CLAUDE_STATS_FILE = Path.home() / ".claude" / "stats-cache.json"


def format_tokens(n: int) -> str:
    """Render a token count compactly (``424M``, ``1.5B``).

    :param n: Token count.
    :return: A short human string.
    """
    if n >= 1_000_000_000:  # noqa: PLR2004
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:  # noqa: PLR2004
        return f"{n / 1_000_000:.0f}M"
    if n >= 1_000:  # noqa: PLR2004
        return f"{n / 1_000:.0f}K"
    return str(n)


def format_since(value: str | None) -> str:
    """Render a date as ``Mon D`` (e.g. ``Dec 28``).

    :param value: ISO date/datetime string, or ``None``.
    :return: ``"Mon D"``, ``""`` for ``None``, or the raw string if
        unparseable.
    """
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.strptime(value[:10], "%Y-%m-%d")
        except ValueError:
            return value
    return f"{dt:%b} {dt.day}"


def claude_lifetime_output() -> tuple[int, str | None]:
    """Sum Claude lifetime output tokens across all local models.

    :return: ``(output_total, since)`` — ``(0, None)`` if the stats
        file is missing or unreadable.
    """
    try:
        data = json.loads(_CLAUDE_STATS_FILE.read_text())
    except OSError, json.JSONDecodeError:
        return (0, None)
    if not isinstance(data, dict):
        return (0, None)
    total = 0
    model_usage = data.get("modelUsage")
    if isinstance(model_usage, dict):
        for usage in model_usage.values():
            if isinstance(usage, dict):
                out = usage.get("outputTokens")
                if isinstance(out, int):
                    total += out
    since = data.get("firstSessionDate")
    return (total, since if isinstance(since, str) else None)


#: Codex session logs and the sidekick-side incremental cache.
_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
_CODEX_CACHE_FILE = (
    Path.home() / ".config" / "sidekick-usages" / "codex-lifetime-cache.json"
)


def _total_token_usage(record: object) -> dict[str, object] | None:
    """Return ``payload.info.total_token_usage`` if present."""
    if not isinstance(record, dict):
        return None
    d: dict[str, object] = cast("dict[str, object]", record)
    payload = d.get("payload")
    if not isinstance(payload, dict):
        return None
    p: dict[str, object] = cast("dict[str, object]", payload)
    info = p.get("info")
    if not isinstance(info, dict):
        return None
    i: dict[str, object] = cast("dict[str, object]", info)
    usage = i.get("total_token_usage")
    return (
        cast("dict[str, object]", usage) if isinstance(usage, dict) else None
    )


def _max_output_in_rollout(path: Path) -> int:
    """Return the max cumulative ``output_tokens`` in one rollout."""
    best = 0
    try:
        with path.open() as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usage = _total_token_usage(record)
                if usage is not None:
                    out = usage.get("output_tokens")
                    if isinstance(out, int) and out > best:
                        best = out
    except OSError:
        return 0
    return best


def _rollout_date(filename: str) -> str | None:
    """Extract ``YYYY-MM-DD`` from a ``rollout-...`` filename."""
    stem = filename.removeprefix("rollout-")
    date = stem[:10]
    return date if len(date) == 10 and date[4] == "-" else None  # noqa: PLR2004


def _load_codex_cache() -> dict[str, object]:
    try:
        data = json.loads(_CODEX_CACHE_FILE.read_text())
    except OSError, json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_codex_cache(cache: dict[str, object]) -> None:
    try:
        _CODEX_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CODEX_CACHE_FILE.write_text(json.dumps(cache))
    except OSError:
        pass


def codex_lifetime_output() -> tuple[int, str | None]:
    """Sum Codex lifetime output tokens across all rollout logs.

    Per file uses the maximum cumulative ``output_tokens`` (the
    session total) and sums across files. Closed sessions are
    immutable, so results are cached per filename+mtime; only new or
    still-growing files are re-read.

    :return: ``(output_total, since)`` — ``(0, None)`` if no logs.
    """
    files = sorted(_CODEX_SESSIONS_DIR.glob("**/rollout-*.jsonl"))
    if not files:
        return (0, None)
    cache = _load_codex_cache()
    raw_entries = cache.get("files")
    entries: dict[str, object] = (
        cast("dict[str, object]", raw_entries)
        if isinstance(raw_entries, dict)
        else {}
    )
    total = 0
    changed = False
    for path in files:
        key = path.name
        mtime = path.stat().st_mtime
        raw_cached = entries.get(key)
        cached: dict[str, object] | None = (
            cast("dict[str, object]", raw_cached)
            if isinstance(raw_cached, dict)
            else None
        )
        if cached is not None and cached.get("mtime") == mtime:
            output_val = cached.get("output", 0)
            output = output_val if isinstance(output_val, int) else 0
        else:
            output = _max_output_in_rollout(path)
            entries[key] = {"mtime": mtime, "output": output}
            changed = True
        total += output
    if changed:
        _save_codex_cache({"files": entries})
    return (total, _rollout_date(files[0].name))

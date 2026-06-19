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

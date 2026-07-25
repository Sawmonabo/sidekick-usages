"""Pure ordering for protected Codex credential generations."""

import re
from datetime import UTC, datetime

type CodexGenerationOrder = tuple[int, int, int, int, int, int, int]

_CODEX_GENERATION_PATTERN = re.compile(
    r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-"
    r"(?P<day>[0-9]{2})T(?P<hour>[0-9]{2}):"
    r"(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?Z\Z",
    re.ASCII,
)


def codex_generation_order(value: str) -> CodexGenerationOrder:
    """Return the exact provider timestamp order without losing nanos."""
    match = _CODEX_GENERATION_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("Codex credential generation is malformed.")
    values = tuple(
        int(match.group(name))
        for name in (
            "year",
            "month",
            "day",
            "hour",
            "minute",
            "second",
        )
    )
    try:
        datetime(*values, tzinfo=UTC)
    except ValueError:
        raise ValueError("Codex credential generation is malformed.") from None
    fraction = match.group("fraction") or ""
    nanoseconds = int(fraction.ljust(9, "0"))
    return (
        values[0],
        values[1],
        values[2],
        values[3],
        values[4],
        values[5],
        nanoseconds,
    )

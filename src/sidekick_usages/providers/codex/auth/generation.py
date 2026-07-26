"""Pure ordering for protected Codex credential generations."""

import re
from datetime import UTC, datetime

from sidekick_usages.core.accounts.types import AuthorityGeneration
from sidekick_usages.core.selection.types import (
    AuthorityGenerationRelation,
)

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


def codex_generation_relation(
    saved: AuthorityGeneration,
    selected: AuthorityGeneration,
) -> AuthorityGenerationRelation:
    """Compare selected Codex time generation with saved authority truth."""
    saved_order = codex_generation_order(str(saved))
    selected_order = codex_generation_order(str(selected))
    if selected_order == saved_order:
        return AuthorityGenerationRelation.CURRENT
    if selected_order < saved_order:
        return AuthorityGenerationRelation.OLDER
    return AuthorityGenerationRelation.NEWER

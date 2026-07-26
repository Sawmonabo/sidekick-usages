"""Validated Codex account observations."""

from dataclasses import dataclass

from sidekick_usages.core.accounts.validation import (
    MAX_METADATA_BYTES,
    require_bounded_text,
)


@dataclass(frozen=True, slots=True)
class CodexAccountObservation:
    """Sanitized non-null ChatGPT account observation."""

    plan: str

    def __post_init__(self) -> None:
        """Require one bounded provider plan."""
        require_bounded_text(
            self.plan,
            name="Codex plan",
            maximum=MAX_METADATA_BYTES,
        )

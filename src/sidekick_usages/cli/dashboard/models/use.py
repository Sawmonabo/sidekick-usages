"""Typed outcomes for scriptable coordinated account selection."""

from dataclasses import dataclass

from sidekick_usages.core.selection.models import safe_outcome_code

type UseSelectionResult = UseSelectionSuccess | UseSelectionFailure


@dataclass(frozen=True, slots=True)
class UseSelectionSuccess:
    """The supervisor made one account ready for next requests."""

    ready_count: int = 0

    def __post_init__(self) -> None:
        """Require a nonnegative participant readiness count."""
        if type(self.ready_count) is not int or self.ready_count < 0:
            raise ValueError("Use selection readiness is invalid.")


@dataclass(frozen=True, slots=True)
class UseSelectionFailure:
    """One sanitized coordinated selection failure."""

    code: str

    def __post_init__(self) -> None:
        """Require one bounded machine-readable failure code."""
        if safe_outcome_code(self.code) is None:
            raise ValueError("Use selection failures require a safe code.")

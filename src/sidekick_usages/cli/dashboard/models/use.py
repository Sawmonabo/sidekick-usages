"""Typed outcomes for scriptable dashboard account activation."""

from dataclasses import dataclass

from sidekick_usages.core.selection.models import safe_outcome_code

type UseActivationResult = UseActivationSuccess | UseActivationFailure


@dataclass(frozen=True, slots=True)
class UseActivationSuccess:
    """The supervisor verified one requested provider activation."""


@dataclass(frozen=True, slots=True)
class UseActivationFailure:
    """One sanitized supervisor activation failure."""

    code: str

    def __post_init__(self) -> None:
        """Require one bounded machine-readable failure code."""
        if safe_outcome_code(self.code) is None:
            raise ValueError("Use activation failures require a safe code.")

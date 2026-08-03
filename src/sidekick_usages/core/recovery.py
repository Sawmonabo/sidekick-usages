"""Provider-neutral bounded recovery-report models."""

from dataclasses import dataclass

from sidekick_usages.core.selection.models import safe_outcome_code

_MAX_PREPARATION_STEPS = 3
_MAX_OPERATOR_STEP_BYTES = 256


@dataclass(frozen=True, slots=True)
class PreparationReport[ReasonT: str]:
    """Bounded token-free dry-run operator recovery guidance."""

    reason: ReasonT
    operator_steps: tuple[str, ...]
    dry_run: bool = True

    def __post_init__(self) -> None:
        """Require one safe bounded dry-run recovery sequence."""
        if (
            safe_outcome_code(self.reason) is None
            or self.dry_run is not True
            or not 1 <= len(self.operator_steps) <= _MAX_PREPARATION_STEPS
            or any(
                not isinstance(step, str)
                or not step
                or len(step.encode("utf-8")) > _MAX_OPERATOR_STEP_BYTES
                for step in self.operator_steps
            )
        ):
            raise ValueError("Preparation report is invalid.")

    @property
    def reason_value(self) -> str:
        """Return the stable provider-neutral reason value."""
        return self.reason

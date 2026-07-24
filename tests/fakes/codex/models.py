"""Models for synthetic Codex provider behavior."""

from dataclasses import dataclass

_LOGIN_OUTCOMES = frozenset({"cancelled", "success"})


@dataclass(frozen=True, slots=True)
class FakeCodexLogin:
    """One official fake-login result for a private Codex home."""

    provider_identity: str
    login_generation: str
    refresh_generation: str
    outcome: str = "success"

    def __post_init__(self) -> None:
        """Reject unsupported fake outcomes."""
        if self.outcome not in _LOGIN_OUTCOMES:
            raise ValueError("Fake Codex login outcome is invalid.")

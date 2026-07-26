"""Models for synthetic Codex app-server behavior."""

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


@dataclass(frozen=True, slots=True)
class FakeCodexRefreshResponse:
    """One secret-free first response to a fake daemon refresh request."""

    responder: str
    account_id: str | None
    error_code: int | None

    def __post_init__(self) -> None:
        """Require exactly one success or error observation."""
        if not self.responder or (
            (self.account_id is None) == (self.error_code is None)
        ):
            raise ValueError("Fake Codex refresh response is invalid.")

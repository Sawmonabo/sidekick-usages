"""Operating-system peer identity models."""

from dataclasses import dataclass

__all__ = ["PeerIdentity"]


@dataclass(frozen=True, slots=True)
class PeerIdentity:
    """Verified operating-system identity for one local connection."""

    effective_user_id: int

    def __post_init__(self) -> None:
        """Require a nonnegative effective user identifier."""
        if (
            isinstance(self.effective_user_id, bool)
            or not isinstance(self.effective_user_id, int)
            or self.effective_user_id < 0
        ):
            raise ValueError("Peer effective user ID is invalid.")

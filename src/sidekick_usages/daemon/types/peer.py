"""Closed operating-system peer verification types."""

from enum import StrEnum

__all__ = ["PeerFailureCode"]


class PeerFailureCode(StrEnum):
    """Safe reasons an operating system could not prove a local peer."""

    DIFFERENT_USER = "different_user"
    FEATURE_DISABLED = "feature_disabled"
    PROOF_UNAVAILABLE = "proof_unavailable"

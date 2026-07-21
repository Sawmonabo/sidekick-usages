"""Typed boundary around the released-v0.6 compatibility verifier."""

from pathlib import Path

from sidekick_usages.persistence.artifacts import FileSnapshot
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.migrations.account import ReleasedV060Verifier
from sidekick_usages.persistence.migrations.errors import (
    ReleasedVerifierBoundaryError,
    VerificationPhase,
)


def verifier_preflight(verifier: ReleasedV060Verifier) -> None:
    """Run the released verifier preflight with typed boundary failures."""
    try:
        verifier.preflight()
    except PersistenceError:
        raise
    except Exception:
        raise ReleasedVerifierBoundaryError(
            VerificationPhase.PREFLIGHT
        ) from None


def verifier_verify(
    verifier: ReleasedV060Verifier,
    path: Path,
    expected: FileSnapshot,
) -> None:
    """Run post-commit verification with typed boundary failures."""
    try:
        verifier.verify(path, expected)
    except PersistenceError:
        raise
    except Exception:
        raise ReleasedVerifierBoundaryError(
            VerificationPhase.POST_COMMIT
        ) from None


__all__ = ["verifier_preflight", "verifier_verify"]

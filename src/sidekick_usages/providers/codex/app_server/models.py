"""Immutable Codex app-server models."""

from dataclasses import dataclass

from sidekick_usages.platform.models import ExecutableProvenance

_SHA256_HEX_LENGTH = 64


@dataclass(frozen=True, slots=True, order=True)
class CodexVersion:
    """One exact semantic Codex CLI version."""

    major: int
    minor: int
    patch: int

    def __post_init__(self) -> None:
        """Reject invalid semantic-version components."""
        if min(self.major, self.minor, self.patch) < 0:
            raise ValueError("Codex version components cannot be negative.")

    def __str__(self) -> str:
        """Render the canonical numeric semantic version."""
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True, slots=True)
class CodexExecutable:
    """One exact executable and its immutable operation provenance."""

    provenance: ExecutableProvenance
    version: CodexVersion


@dataclass(frozen=True, slots=True)
class CodexAppServerCapabilities:
    """One exact executable proven against required generated schemas."""

    executable: CodexExecutable
    schema_hash: str

    def __post_init__(self) -> None:
        """Require a lowercase SHA-256 compatibility fingerprint."""
        if len(self.schema_hash) != _SHA256_HEX_LENGTH or any(
            character not in "0123456789abcdef"
            for character in self.schema_hash
        ):
            raise ValueError("Codex schema fingerprint is invalid.")

"""Immutable Codex app-server models."""

from dataclasses import dataclass
from pathlib import Path

from sidekick_usages.platform.models import ExecutableProvenance

_SHA256_HEX_LENGTH = 64
_SEMANTIC_VERSION_COMPONENTS = 3


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

    @classmethod
    def parse(cls, value: str) -> CodexVersion:
        """Parse one canonical numeric semantic version."""
        components = value.split(".")
        if len(components) != _SEMANTIC_VERSION_COMPONENTS or any(
            not component.isascii() or not component.isdecimal()
            for component in components
        ):
            raise ValueError("Codex version is invalid.")
        major, minor, patch = (int(component) for component in components)
        return cls(major, minor, patch)


@dataclass(frozen=True, slots=True)
class CodexExecutable:
    """One exact executable and its immutable operation provenance."""

    launcher: Path
    provenance: ExecutableProvenance
    version: CodexVersion

    def __post_init__(self) -> None:
        """Require the stable launcher path preserved at discovery."""
        if not self.launcher.is_absolute():
            raise ValueError("Codex launcher path must be absolute.")


@dataclass(frozen=True, slots=True)
class CodexAppServerCapabilities:
    """One exact executable proven against required generated schemas."""

    executable: CodexExecutable
    schema_hash: str
    session_schema_supported: bool = False
    session_schema_manifest: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Require a lowercase SHA-256 compatibility fingerprint."""
        if len(self.schema_hash) != _SHA256_HEX_LENGTH or any(
            character not in "0123456789abcdef"
            for character in self.schema_hash
        ):
            raise ValueError("Codex schema fingerprint is invalid.")
        if self.session_schema_supported != bool(self.session_schema_manifest):
            raise ValueError("Codex session schema manifest is inconsistent.")

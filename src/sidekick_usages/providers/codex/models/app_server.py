"""Immutable models for the proven Codex app-server boundary."""

from dataclasses import dataclass, field
from pathlib import Path

from sidekick_usages.serialization import JsonObject

_SHA256_HEX_LENGTH = 64

__all__ = [
    "CodexAppServerCapabilities",
    "CodexExecutable",
    "CodexVersion",
    "JsonRpcErrorResponse",
    "JsonRpcNotification",
    "JsonRpcResponse",
    "JsonRpcServerRequest",
]


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

    path: Path
    version: CodexVersion
    device: int
    inode: int
    size: int
    modified_nanoseconds: int

    def __post_init__(self) -> None:
        """Require an absolute provenance path and valid file identity."""
        if not self.path.is_absolute():
            raise ValueError("Codex executable provenance must be absolute.")
        if (
            min(
                self.device,
                self.inode,
                self.size,
                self.modified_nanoseconds,
            )
            < 0
        ):
            raise ValueError("Codex executable provenance is invalid.")


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


@dataclass(frozen=True, slots=True)
class JsonRpcResponse:
    """One successful response correlated to a client request."""

    request_id: int
    result: JsonObject = field(repr=False)


@dataclass(frozen=True, slots=True)
class JsonRpcErrorResponse:
    """One redacted server error correlated to a client request."""

    request_id: int
    code: int


@dataclass(frozen=True, slots=True)
class JsonRpcNotification:
    """One server notification with protected parameters."""

    method: str
    params: JsonObject = field(repr=False)


@dataclass(frozen=True, slots=True)
class JsonRpcServerRequest:
    """One server-initiated request with protected parameters."""

    request_id: int | str
    method: str
    params: JsonObject = field(repr=False)

"""Current persistence artifact naming and namespace validation."""

import ntpath
import re
import secrets
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from sidekick_usages.persistence.models.artifact import ManagedArtifact
from sidekick_usages.persistence.types.artifact import (
    ArtifactPurpose,
    ManagedArtifactKind,
)

TEMPORARY_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class ArtifactGrammar:
    """Closed current artifact grammar for one authority basename."""

    authority_basename: str
    _temporary_pattern: re.Pattern[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        require_safe_basename(self.authority_basename)
        escaped = re.escape(self.authority_basename)
        object.__setattr__(
            self,
            "_temporary_pattern",
            re.compile(rf"\.{escaped}\.authority\.([0-9a-f]{{32}})\.tmp\Z"),
        )

    @property
    def lock_basename(self) -> str:
        """Return the one persistent lock-sidecar basename."""
        return f"{self.authority_basename}.lock"

    def temporary_basename(self, purpose: ArtifactPurpose) -> str:
        """Return a fresh current-authority temporary basename."""
        if purpose is not ArtifactPurpose.AUTHORITY:
            raise ValueError("Unsupported temporary artifact purpose.")
        return (
            f".{self.authority_basename}.authority.{secrets.token_hex(16)}.tmp"
        )

    def parse(self, basename: str) -> ManagedArtifact | None:
        """Classify one exact current basename without opening it."""
        if not is_safe_basename(basename):
            return None
        if basename == self.authority_basename:
            return ManagedArtifact(ManagedArtifactKind.AUTHORITY, basename)
        if basename == self.lock_basename:
            return ManagedArtifact(ManagedArtifactKind.LOCK, basename)
        match = self._temporary_pattern.fullmatch(basename)
        if (
            match is None
            or TEMPORARY_TOKEN_PATTERN.fullmatch(match.group(1)) is None
        ):
            return None
        return ManagedArtifact(
            ManagedArtifactKind.TEMPORARY,
            basename,
            purpose=ArtifactPurpose.AUTHORITY,
        )


def is_safe_basename(value: str) -> bool:
    """Return whether one injected basename is safe for relative use."""
    return (
        bool(value)
        and value not in {".", ".."}
        and value == value.rstrip(" .")
        and not (
            "/" in value
            or "\\" in value
            or any(
                unicodedata.category(character) == "Cc" for character in value
            )
        )
    )


def portable_basename_key(value: str) -> str:
    """Return the Windows-compatible identity of one safe basename."""
    return ntpath.normcase(value.rstrip(" ."))


def require_portable_unique_basenames(values: Iterable[str]) -> None:
    """Reject names that alias in a portable filesystem namespace."""
    keys = tuple(portable_basename_key(value) for value in values)
    if len(keys) != len(set(keys)):
        raise ValueError(
            "Artifact names must be unique in the portable namespace."
        )


def require_safe_basename(value: str) -> None:
    """Reject a basename that can escape or confuse the parent namespace."""
    if not is_safe_basename(value):
        raise ValueError("Authority path must have one safe basename.")

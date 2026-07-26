"""Typed Rich-free branding layout models."""

from dataclasses import dataclass
from enum import StrEnum


class BrandTextRole(StrEnum):
    """Closed visual roles in the canonical terminal masthead."""

    PLAIN = "plain"
    ROBOT = "robot"
    TITLE = "title"
    PRODUCT = "product"
    DESCRIPTION = "description"
    PROMISE = "promise"
    CLAUDE = "claude"
    CODEX = "codex"
    DIVIDER = "divider"


@dataclass(frozen=True, slots=True)
class BrandText:
    """One masthead text segment with its semantic visual role."""

    value: str
    role: BrandTextRole


@dataclass(frozen=True, slots=True)
class BrandLine:
    """One canonical masthead line composed from semantic text."""

    segments: tuple[BrandText, ...]

    @property
    def plain(self) -> str:
        """Return the exact unstyled masthead text."""
        return "".join(segment.value for segment in self.segments)

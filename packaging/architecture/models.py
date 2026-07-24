"""Typed architecture-checker data models."""

import ast
from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, slots=True, order=True)
class ArchitectureFinding:
    """One stable architecture diagnostic."""

    path: PurePosixPath
    line: int
    rule_id: str
    message: str

    def render(self) -> str:
        """Render a terminal-friendly diagnostic."""
        return f"{self.path}:{self.line}: {self.rule_id} {self.message}"


@dataclass(frozen=True, slots=True)
class ArchitectureReport:
    """Complete errors and review warnings for one repository snapshot."""

    violations: tuple[ArchitectureFinding, ...]
    warnings: tuple[ArchitectureFinding, ...]


@dataclass(frozen=True, slots=True)
class SourceUnit:
    """One parsed repository source file."""

    path: PurePosixPath
    source: str
    tree: ast.Module

    @property
    def production(self) -> bool:
        """Return whether the source belongs to the installed package."""
        return self.path.parts[:2] == ("src", "sidekick_usages")

    @property
    def packaging(self) -> bool:
        """Return whether the source belongs to packaging tooling."""
        return bool(self.path.parts) and self.path.parts[0] == "packaging"


@dataclass(frozen=True, slots=True)
class SourceMutation:
    """One deliberate source change used to prove an architecture rule."""

    rule_id: str
    path: str
    original: str
    replacement: str

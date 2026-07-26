"""Exact-distribution verification models."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectContract:
    """One validated project and wheel-source declaration."""

    repository_root: Path
    package_root: Path
    distribution_name: str
    version: str
    scripts: tuple[tuple[str, str], ...]

"""Repository size and project-metadata architecture contracts."""

import tomllib
from collections.abc import Sequence
from pathlib import PurePosixPath

from architecture.models import ArchitectureFinding, SourceUnit
from architecture.source import finding

MAX_MODULE_LINES = 1000
REVIEW_MODULE_LINES = 800


def check_source_sizes(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
    warnings: list[ArchitectureFinding],
) -> None:
    """Enforce the hard module limit and cohesion review threshold."""
    for unit in units:
        lines = len(unit.source.splitlines())
        if lines > MAX_MODULE_LINES:
            violations.append(
                finding(
                    unit,
                    None,
                    "SIZE001",
                    f"module has {lines} lines; limit is {MAX_MODULE_LINES}",
                )
            )
        elif lines >= REVIEW_MODULE_LINES:
            warnings.append(
                finding(
                    unit,
                    None,
                    "SIZE002",
                    f"module has {lines} lines; review cohesion",
                )
            )


def check_project_contract(
    project_text: str,
    violations: list[ArchitectureFinding],
) -> None:
    """Reject unapproved project-level dependencies."""
    project = tomllib.loads(project_text)
    dependencies = project.get("project", {}).get("dependencies", ())
    has_settings = any(
        isinstance(dependency, str)
        and dependency.split(";", maxsplit=1)[0]
        .strip()
        .lower()
        .startswith("pydantic-settings")
        for dependency in dependencies
    )
    if has_settings:
        violations.append(
            ArchitectureFinding(
                PurePosixPath("pyproject.toml"),
                1,
                "CFG001",
                "pydantic-settings requires a separately approved contract",
            )
        )

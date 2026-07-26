#!/usr/bin/env python3
"""Enforce Sidekick's repository-specific architecture contracts."""

import sys
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

from architecture.models import ArchitectureFinding, ArchitectureReport
from architecture.rules.claude import check_claude_auth_ownership
from architecture.rules.cli import check_cli_contract
from architecture.rules.codex import check_codex_auth_ownership
from architecture.rules.dependencies import check_import_boundaries
from architecture.rules.hygiene import check_hygiene
from architecture.rules.ownership import check_ownership
from architecture.rules.repository import (
    check_project_contract,
    check_source_sizes,
)
from architecture.rules.runtime import (
    check_activity_contract,
    check_time_and_settings,
)
from architecture.rules.shape import check_source_shape
from architecture.rules.values import check_value_contracts
from architecture.source import load_units

REPO_ROOT = Path(__file__).resolve().parents[1]


def check_repository(
    root: Path = REPO_ROOT,
    *,
    source_overrides: Mapping[str, str] | None = None,
    pyproject_override: str | None = None,
) -> ArchitectureReport:
    """Check the repository with optional in-memory test mutations."""
    overrides = {
        PurePosixPath(path): source
        for path, source in (source_overrides or {}).items()
    }
    units = load_units(root, overrides)
    violations: list[ArchitectureFinding] = []
    warnings: list[ArchitectureFinding] = []

    check_source_sizes(units, violations, warnings)
    check_hygiene(units, violations)
    check_import_boundaries(units, violations)
    check_time_and_settings(units, violations)
    check_value_contracts(units, violations)
    check_activity_contract(units, violations)
    check_claude_auth_ownership(units, violations)
    check_codex_auth_ownership(units, violations)
    check_cli_contract(units, violations)
    check_ownership(units, violations)
    check_source_shape(units, violations)
    project_text = pyproject_override or root.joinpath(
        "pyproject.toml"
    ).read_text(encoding="utf-8")
    check_project_contract(project_text, violations)
    return ArchitectureReport(
        tuple(sorted(set(violations))),
        tuple(sorted(set(warnings))),
    )


def main() -> int:
    """Check the current checkout and emit stable diagnostics."""
    report = check_repository()
    for warning in report.warnings:
        sys.stderr.write(f"warning: {warning.render()}\n")
    for violation in report.violations:
        sys.stderr.write(f"error: {violation.render()}\n")
    if report.violations:
        sys.stderr.write(
            "architecture check failed: "
            f"{len(report.violations)} violation(s)\n"
        )
        return 1
    sys.stdout.write(
        "Architecture check passed with "
        f"{len(report.warnings)} cohesion warning(s).\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

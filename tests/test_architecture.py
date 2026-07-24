"""Load-bearing architecture and command-surface contracts."""

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Protocol

import click
import pytest
from typer.main import get_command

from sidekick_usages.cli.app import create_app

REPO_ROOT = Path(__file__).parents[1]
PACKAGING_ROOT = REPO_ROOT / "packaging"


def _load_architecture_tools() -> tuple[ModuleType, ModuleType]:
    """Load repo tooling without making packaging an application package."""
    sys.path.insert(0, str(PACKAGING_ROOT))
    try:
        checker = importlib.import_module("check_architecture")
        support = importlib.import_module("architecture_ast")
    finally:
        sys.path.remove(str(PACKAGING_ROOT))
    return checker, support


architecture, architecture_ast = _load_architecture_tools()


@dataclass(frozen=True, slots=True)
class _Mutation:
    rule_id: str
    path: str
    original: str
    replacement: str


class _Finding(Protocol):
    rule_id: str


class _Report(Protocol):
    violations: tuple[_Finding, ...]


_MUTATIONS = (
    _Mutation(
        "SIZE001",
        "tests/oversized_architecture_fixture.py",
        "",
        "# deliberate line\n" * 1001,
    ),
    _Mutation(
        "HYG001",
        "tests/architecture_any_fixture.py",
        "",
        "from typing import Any\nvalue: Any\n",
    ),
    _Mutation(
        "HYG002",
        "tests/architecture_future_fixture.py",
        "",
        "from __future__ import annotations\n",
    ),
    _Mutation(
        "HYG003",
        "tests/architecture_suppression_fixture.py",
        "",
        "value = 1  # no" + "qa\n",
    ),
    _Mutation(
        "HYG004",
        "tests/architecture_exception_fixture.py",
        "",
        "try:\n    raise RuntimeError\nexcept Exception:\n    pass\n",
    ),
    _Mutation(
        "DEP001",
        "src/sidekick_usages/core/architecture_fixture.py",
        "",
        "import rich\n",
    ),
    _Mutation(
        "DEP002",
        "src/sidekick_usages/architecture_fixture.py",
        "",
        "from sidekick_usages.cli import context\n",
    ),
    _Mutation(
        "DEP003",
        "src/sidekick_usages/persistence/architecture_fixture.py",
        "",
        "from sidekick_usages.providers import base\n",
    ),
    _Mutation(
        "DEP004",
        "src/sidekick_usages/providers/architecture_fixture.py",
        "",
        "from sidekick_usages.usage import service\n",
    ),
    _Mutation(
        "DEP005",
        "src/sidekick_usages/http/architecture_fixture.py",
        "",
        "from sidekick_usages.providers import base\n",
    ),
    _Mutation(
        "DEP006",
        "src/sidekick_usages/usage/service.py",
        "from dataclasses import dataclass\n",
        ("from dataclasses import dataclass\nfrom rich.text import Text\n"),
    ),
    _Mutation(
        "DEP007",
        "src/sidekick_usages/architecture_transport_fixture.py",
        "",
        "import urllib3\n",
    ),
    _Mutation(
        "DEP008",
        "src/sidekick_usages/usage/render.py",
        "from rich.console import Console, Group, RenderableType\n",
        (
            "from rich.console import Console, Group, RenderableType\n"
            "from sidekick_usages.credentials import authorities\n"
        ),
    ),
    _Mutation(
        "PATH001",
        "src/sidekick_usages/architecture_paths_fixture.py",
        "",
        "import platformdirs\n",
    ),
    _Mutation(
        "PATH002",
        "src/sidekick_usages/architecture_path_owner_fixture.py",
        "",
        "class ApplicationPaths:\n    pass\n",
    ),
    _Mutation(
        "TIME001",
        "src/sidekick_usages/usage/architecture_time_fixture.py",
        "",
        "from datetime import datetime\nnow = datetime.now()\n",
    ),
    _Mutation(
        "TIME002",
        "src/sidekick_usages/core/time.py",
        "    return value.astimezone(UTC)\n",
        (
            "    return value.astimezone(UTC)\n\n\n"
            "def parse_timestamp(value: str) -> datetime:\n"
            "    return datetime.fromisoformat(value)\n"
        ),
    ),
    _Mutation(
        "CFG001",
        "pyproject.toml",
        "dependencies = [\n",
        'dependencies = [\n  "pydantic-settings==2.14.2",\n',
    ),
    _Mutation(
        "CTX001",
        "src/sidekick_usages/cli/context.py",
        "    accounts: AccountStore\n",
        "    paths: ApplicationPaths\n",
    ),
    _Mutation(
        "CTX002",
        "src/sidekick_usages/cli/context.py",
        "            ctx.find_root().call_on_close(owner.close)\n",
        (
            "            ctx.find_root().call_on_close(owner.close)\n"
            "            ctx.find_root().call_on_close(owner.close)\n"
        ),
    ),
    _Mutation(
        "CLI001",
        "src/sidekick_usages/cli/app.py",
        "    return application\n",
        "    compose_app_context()\n    return application\n",
    ),
    _Mutation(
        "MIG001",
        "src/sidekick_usages/persistence/architecture_migration_fixture.py",
        "",
        "transaction.commit_migration()\n",
    ),
    _Mutation(
        "HTTP001",
        "src/sidekick_usages/architecture_retry_fixture.py",
        "",
        "_POLICIES = {}\n",
    ),
    _Mutation(
        "BRAND001",
        "src/sidekick_usages/architecture_brand_fixture.py",
        "",
        "ROBOT_LINES = ()\n",
    ),
    _Mutation(
        "PKG001",
        "src/sidekick_usages/render.py",
        "",
        '"""Stale converted module."""\n',
    ),
    _Mutation(
        "SCHEMA001",
        "src/sidekick_usages/architecture_schema_fixture.py",
        "",
        "from pydantic import TypeAdapter\n",
    ),
    _Mutation(
        "MODEL001",
        "src/sidekick_usages/persistence/migrations/location.py",
        '    EMPTY = "empty"\n',
        '    EMPTY = "emptied"\n',
    ),
    _Mutation(
        "ACT001",
        "src/sidekick_usages/activity_architecture_fixture.py",
        "",
        "import sidekick_usages.lifetime\n",
    ),
)


def test_real_tree_satisfies_every_static_architecture_contract() -> None:
    """The consolidated gate accepts the complete repository snapshot."""
    report = architecture.check_repository(REPO_ROOT)

    assert report.violations == ()


def _deliberately_broken_report() -> _Report:
    """Apply one precise violation per rule to a single in-memory snapshot."""
    source_overrides: dict[str, str] = {}
    pyproject_override = (REPO_ROOT / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    for mutation in _MUTATIONS:
        target = REPO_ROOT / mutation.path
        if mutation.path == "pyproject.toml":
            assert pyproject_override.count(mutation.original) == 1
            pyproject_override = pyproject_override.replace(
                mutation.original,
                mutation.replacement,
                1,
            )
            continue
        if not target.exists() and mutation.path not in source_overrides:
            assert mutation.original == ""
            source_overrides[mutation.path] = mutation.replacement
            continue
        source = source_overrides.get(
            mutation.path,
            target.read_text(encoding="utf-8"),
        )
        assert source.count(mutation.original) == 1
        source_overrides[mutation.path] = source.replace(
            mutation.original,
            mutation.replacement,
            1,
        )
    return architecture.check_repository(
        REPO_ROOT,
        source_overrides=source_overrides,
        pyproject_override=pyproject_override,
    )


def test_every_static_rule_rejects_a_deliberate_violation() -> None:
    """Every advertised failure rule is executable, not documentation."""
    report = _deliberately_broken_report()

    assert {violation.rule_id for violation in report.violations} == (
        architecture_ast.VIOLATION_RULE_IDS
    )
    assert {mutation.rule_id for mutation in _MUTATIONS} == (
        architecture_ast.VIOLATION_RULE_IDS
    )


def test_near_limit_module_emits_a_cohesion_warning() -> None:
    """The review threshold warns without pretending to be a hard failure."""
    report = architecture.check_repository(
        REPO_ROOT,
        source_overrides={
            "tests/architecture_warning_fixture.py": "# review\n" * 800,
        },
    )

    assert any(
        warning.rule_id == "SIZE002"
        and warning.path.name == "architecture_warning_fixture.py"
        for warning in report.warnings
    )
    assert not any(
        violation.path.name == "architecture_warning_fixture.py"
        for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "source", "rule_id"),
    [
        (
            "src/sidekick_usages/core/cross_owner_fixture.py",
            "from sidekick_usages.credentials import models\n",
            "DEP001",
        ),
        (
            "src/sidekick_usages/persistence/cross_owner_fixture.py",
            "from sidekick_usages.credentials import models\n",
            "DEP003",
        ),
        (
            "src/sidekick_usages/providers/cross_owner_fixture.py",
            "from sidekick_usages.credentials import models\n",
            "DEP004",
        ),
        (
            "src/sidekick_usages/credentials/cross_owner_fixture.py",
            "from sidekick_usages.usage import service\n",
            "DEP006",
        ),
    ],
)
def test_concrete_owner_boundaries_reject_reverse_dependencies(
    path: str,
    source: str,
    rule_id: str,
) -> None:
    """Final ownership layers stay directed."""
    report = architecture.check_repository(
        REPO_ROOT,
        source_overrides={path: source},
    )

    assert any(
        violation.path.as_posix() == path and violation.rule_id == rule_id
        for violation in report.violations
    )


@pytest.mark.parametrize(
    ("path", "source", "rule_id"),
    [
        (
            "src/sidekick_usages/persistence/import_fixture.py",
            "from sidekick_usages import providers as boundary\n",
            "DEP003",
        ),
        (
            "src/sidekick_usages/persistence/migrations/import_fixture.py",
            "from ... import providers as boundary\n",
            "DEP003",
        ),
        (
            "src/sidekick_usages/providers/import_fixture.py",
            "from sidekick_usages import credentials as boundary\n",
            "DEP004",
        ),
        (
            "src/sidekick_usages/providers/claude/import_fixture.py",
            "from ... import credentials as boundary\n",
            "DEP004",
        ),
        (
            "src/sidekick_usages/credentials/import_fixture.py",
            "from sidekick_usages import usage as boundary\n",
            "DEP006",
        ),
        (
            "src/sidekick_usages/credentials/import_fixture.py",
            "from .. import usage as boundary\n",
            "DEP006",
        ),
        (
            "src/sidekick_usages/http/import_fixture.py",
            "from sidekick_usages import providers as boundary\n",
            "DEP005",
        ),
        (
            "src/sidekick_usages/http/import_fixture.py",
            "from .. import providers as boundary\n",
            "DEP005",
        ),
        (
            "src/sidekick_usages/import_fixture.py",
            "from sidekick_usages import cli as boundary\n",
            "DEP002",
        ),
        (
            "src/sidekick_usages/import_fixture.py",
            "from . import cli as boundary\n",
            "DEP002",
        ),
    ],
)
def test_import_from_aliases_and_levels_cannot_bypass_boundaries(
    path: str,
    source: str,
    rule_id: str,
) -> None:
    """Root aliases and relative imports resolve before policy checks."""
    report = architecture.check_repository(
        REPO_ROOT,
        source_overrides={path: source},
    )

    assert any(
        violation.path.as_posix() == path and violation.rule_id == rule_id
        for violation in report.violations
    )


def test_cli_command_surface_is_registered_once_by_focused_owners() -> None:
    """Application assembly exposes the complete intentional command tree."""
    root = get_command(create_app())
    assert isinstance(root, click.Group)
    assert set(root.commands) == {
        "add",
        "check",
        "check-update",
        "claude",
        "codex",
        "codex-export",
        "codex-login",
        "daemon",
        "doctor",
        "heartbeat",
        "list",
        "maintain",
        "migrate",
        "permissions",
        "refresh",
        "remove",
        "rename",
        "reset",
        "set-plan",
        "setup-token",
        "update",
    }
    nested = {
        name: set(command.commands)
        for name, command in root.commands.items()
        if isinstance(command, click.Group)
    }
    assert nested == {
        "claude": {"restore-setup-token", "setup-token"},
        "codex": {"export", "login"},
        "daemon": {"install", "status", "uninstall"},
        "heartbeat": {"disable", "enable", "run-label", "status"},
        "migrate": {"accounts", "locations", "prepare-rollback"},
        "permissions": {"repair"},
    }

"""Load-bearing architecture and command-surface contracts."""

from pathlib import Path

import click
import pytest
from typer.main import get_command

import architecture.models
import check_architecture
from sidekick_usages.cli.app import create_app

REPO_ROOT = Path(__file__).parents[1]
_RICH_FREE_STARTUP_PATH = "src/sidekick_usages/cli/runtime/routing.py"


_MUTATIONS = (
    architecture.models.SourceMutation(
        "SIZE001",
        "tests/oversized_architecture_fixture.py",
        "",
        "# deliberate line\n" * 1001,
    ),
    architecture.models.SourceMutation(
        "HYG001",
        "tests/architecture_any_fixture.py",
        "",
        "from typing import Any\nvalue: Any\n",
    ),
    architecture.models.SourceMutation(
        "HYG002",
        "tests/architecture_future_fixture.py",
        "",
        "from __future__ import annotations\n",
    ),
    architecture.models.SourceMutation(
        "HYG003",
        "tests/architecture_suppression_fixture.py",
        "",
        "value = 1  # no" + "qa\n",
    ),
    architecture.models.SourceMutation(
        "HYG004",
        "tests/architecture_exception_fixture.py",
        "",
        "try:\n    raise RuntimeError\nexcept Exception:\n    pass\n",
    ),
    architecture.models.SourceMutation(
        "HYG005",
        "tests/architecture_nested_import_fixture.py",
        "",
        "def load() -> None:\n    import pathlib\n",
    ),
    architecture.models.SourceMutation(
        "HYG006",
        "tests/architecture_alias_fixture.py",
        "",
        "import pathlib as paths\n",
    ),
    architecture.models.SourceMutation(
        "HYG007",
        "tests/architecture_late_declaration_fixture.py",
        "",
        "def load() -> None:\n    pass\n\nVALUE = 1\n",
    ),
    architecture.models.SourceMutation(
        "DEP001",
        "src/sidekick_usages/core/architecture_fixture.py",
        "",
        "import rich\n",
    ),
    architecture.models.SourceMutation(
        "DEP002",
        "src/sidekick_usages/architecture_fixture.py",
        "",
        "from sidekick_usages.cli import context\n",
    ),
    architecture.models.SourceMutation(
        "DEP003",
        "src/sidekick_usages/persistence/architecture_fixture.py",
        "",
        "from sidekick_usages.providers import base\n",
    ),
    architecture.models.SourceMutation(
        "DEP004",
        "src/sidekick_usages/providers/architecture_fixture.py",
        "",
        "from sidekick_usages.usage import service\n",
    ),
    architecture.models.SourceMutation(
        "DEP005",
        "src/sidekick_usages/http/architecture_fixture.py",
        "",
        "from sidekick_usages.providers import base\n",
    ),
    architecture.models.SourceMutation(
        "DEP006",
        "src/sidekick_usages/usage/service.py",
        "from dataclasses import replace\n",
        ("from dataclasses import replace\nfrom rich.text import Text\n"),
    ),
    architecture.models.SourceMutation(
        "DEP006",
        "src/sidekick_usages/usage/dashboard/service.py",
        "from sidekick_usages.paths import ApplicationPaths\n",
        (
            "from sidekick_usages.paths import ApplicationPaths\n"
            "from sidekick_usages.credentials import service\n"
        ),
    ),
    architecture.models.SourceMutation(
        "DEP006",
        "src/sidekick_usages/cli/commands/usage.py",
        "import typer\n",
        "import prompt_toolkit\nimport typer\n",
    ),
    architecture.models.SourceMutation(
        "DEP007",
        "src/sidekick_usages/architecture_transport_fixture.py",
        "",
        "import urllib3\n",
    ),
    architecture.models.SourceMutation(
        "DEP008",
        "src/sidekick_usages/usage/presentation/overview.py",
        "from rich.console import Console, Group, RenderableType\n",
        (
            "from rich.console import Console, Group, RenderableType\n"
            "from sidekick_usages.credentials import authorities\n"
        ),
    ),
    architecture.models.SourceMutation(
        "PATH001",
        "src/sidekick_usages/architecture_paths_fixture.py",
        "",
        "import platformdirs\n",
    ),
    architecture.models.SourceMutation(
        "PATH002",
        "src/sidekick_usages/architecture_path_owner_fixture.py",
        "",
        "class ApplicationPaths:\n    pass\n",
    ),
    architecture.models.SourceMutation(
        "TIME001",
        "src/sidekick_usages/usage/architecture_time_fixture.py",
        "",
        "from datetime import datetime\nnow = datetime.now()\n",
    ),
    architecture.models.SourceMutation(
        "TIME002",
        "src/sidekick_usages/core/time.py",
        "    return value.astimezone(UTC)\n",
        (
            "    return value.astimezone(UTC)\n\n\n"
            "def parse_timestamp(value: str) -> datetime:\n"
            "    return datetime.fromisoformat(value)\n"
        ),
    ),
    architecture.models.SourceMutation(
        "CFG001",
        "pyproject.toml",
        "dependencies = [\n",
        'dependencies = [\n  "pydantic-settings==2.14.2",\n',
    ),
    architecture.models.SourceMutation(
        "CTX001",
        "src/sidekick_usages/cli/contexts/models.py",
        "    accounts: AccountStore\n",
        "    paths: ApplicationPaths\n",
    ),
    architecture.models.SourceMutation(
        "CTX002",
        "src/sidekick_usages/cli/context.py",
        "            ctx.find_root().call_on_close(owner.close)\n",
        (
            "            ctx.find_root().call_on_close(owner.close)\n"
            "            ctx.find_root().call_on_close(owner.close)\n"
        ),
    ),
    architecture.models.SourceMutation(
        "CLI001",
        "src/sidekick_usages/cli/app.py",
        "    return application\n",
        "    compose_app_context()\n    return application\n",
    ),
    architecture.models.SourceMutation(
        "CLI001",
        _RICH_FREE_STARTUP_PATH,
        "from collections.abc import Sequence\n",
        ("import rich\n\nfrom collections.abc import Sequence\n"),
    ),
    architecture.models.SourceMutation(
        "CLI001",
        "src/sidekick_usages/cli/runtime/bootstrap.py",
        "    return os.execve(executable, command, environment)\n",
        "    raise RuntimeError\n",
    ),
    architecture.models.SourceMutation(
        "HTTP001",
        "src/sidekick_usages/architecture_retry_fixture.py",
        "",
        "_POLICIES = {}\n",
    ),
    architecture.models.SourceMutation(
        "BRAND001",
        "src/sidekick_usages/architecture_brand_fixture.py",
        "",
        "ROBOT_LINES = ()\n",
    ),
    architecture.models.SourceMutation(
        "PKG001",
        "src/sidekick_usages/usage/__init__.py",
        '"""Usage collection, aggregation, and presentation."""\n',
        (
            '"""Usage collection, aggregation, and presentation."""\n\n'
            "from sidekick_usages.usage import models\n"
        ),
    ),
    architecture.models.SourceMutation(
        "PKG001",
        "src/sidekick_usages/_internal/fixture.py",
        "",
        '"""Deliberate private package fixture."""\n',
    ),
    architecture.models.SourceMutation(
        "PKG002",
        "src/sidekick_usages/usage/activity_extra.py",
        "",
        '"""Deliberate flat namespace fixture."""\n',
    ),
    architecture.models.SourceMutation(
        "PKG003",
        "src/sidekick_usages/misc_types.py",
        "",
        '"""Deliberate owner-module naming violation."""\n',
    ),
    architecture.models.SourceMutation(
        "SCHEMA001",
        "src/sidekick_usages/architecture_schema_fixture.py",
        "",
        "from pydantic import TypeAdapter\n",
    ),
    architecture.models.SourceMutation(
        "ACT001",
        "src/sidekick_usages/activity_architecture_fixture.py",
        "",
        "import sidekick_usages.lifetime\n",
    ),
    architecture.models.SourceMutation(
        "CODEX001",
        "src/sidekick_usages/providers/codex/retired_auth_fixture.py",
        "",
        'TOKEN_ENDPOINT = "https://auth.openai.com/oauth/token"\n',
    ),
    architecture.models.SourceMutation(
        "CLAUDE001",
        "src/sidekick_usages/providers/claude/retired_auth_fixture.py",
        "",
        'CLAUDE_CONFIG_DIR = "CLAUDE_CONFIG_DIR"\n',
    ),
)
_MUTATION_RULE_IDS = frozenset(mutation.rule_id for mutation in _MUTATIONS)


def test_real_tree_satisfies_every_static_architecture_contract() -> None:
    """The consolidated gate accepts the complete repository snapshot."""
    report = check_architecture.check_repository(REPO_ROOT)

    assert report.violations == ()


def _deliberately_broken_report() -> architecture.models.ArchitectureReport:
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
    return check_architecture.check_repository(
        REPO_ROOT,
        source_overrides=source_overrides,
        pyproject_override=pyproject_override,
    )


def test_every_static_rule_rejects_a_deliberate_violation() -> None:
    """Every advertised failure rule is executable, not documentation."""
    report = _deliberately_broken_report()

    assert {
        violation.rule_id for violation in report.violations
    } == _MUTATION_RULE_IDS
    assert any(
        violation.rule_id == "PKG001"
        and violation.path.as_posix()
        == "src/sidekick_usages/_internal/fixture.py"
        for violation in report.violations
    )
    assert any(
        violation.rule_id == "CLI001"
        and violation.path.as_posix() == _RICH_FREE_STARTUP_PATH
        for violation in report.violations
    )


def test_near_limit_module_emits_a_cohesion_warning() -> None:
    """The review threshold warns without pretending to be a hard failure."""
    report = check_architecture.check_repository(
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


def test_selection_schema_flat_namespace_exception_is_exact() -> None:
    """A third selection schema sibling remains an architecture failure."""
    path = "src/sidekick_usages/persistence/schema/selection_extra.py"
    report = check_architecture.check_repository(
        REPO_ROOT,
        source_overrides={path: '"""Deliberate selection sibling."""\n'},
    )

    assert any(
        violation.rule_id == "PKG002"
        and violation.path.as_posix()
        == "src/sidekick_usages/persistence/schema/selection.py"
        and "selection_operation.py" in violation.message
        and "selection_extra.py" in violation.message
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
    report = check_architecture.check_repository(
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
            "src/sidekick_usages/persistence/schema/import_fixture.py",
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
    report = check_architecture.check_repository(
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
        "update",
        "use",
    }
    nested = {
        name: set(command.commands)
        for name, command in root.commands.items()
        if isinstance(command, click.Group)
    }
    assert nested == {
        "claude": {"setup-token"},
        "codex": {"login"},
        "daemon": {"install", "status", "uninstall"},
        "heartbeat": {"disable", "enable", "run-label", "status"},
        "migrate": {"managed-auth"},
        "permissions": {"repair"},
    }

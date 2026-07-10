"""Load-bearing source dependency boundaries."""

import ast
from collections.abc import Iterator
from pathlib import Path

import click
from typer.main import get_command

from sidekick_usages.cli.app import create_app


def _source_imports(
    source: str,
    *,
    filename: str = "<source>",
) -> Iterator[tuple[int, str]]:
    tree = ast.parse(source, filename=filename)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            yield node.lineno, node.module


def _imports(source: Path) -> Iterator[tuple[int, str]]:
    yield from _source_imports(
        source.read_text(encoding="utf-8"),
        filename=str(source),
    )


def test_import_scanner_finds_direct_and_from_imports() -> None:
    """The architecture scanner detects both supported import forms."""
    assert tuple(
        _source_imports("import pydantic\nfrom pydantic import BaseModel\n")
    ) == ((1, "pydantic"), (2, "pydantic"))


def test_persistence_has_no_provider_dependency() -> None:
    """Persistence remains provider-neutral across every source module."""
    package = (
        Path(__file__).parents[1] / "src" / "sidekick_usages" / "persistence"
    )
    violations = [
        f"{source.relative_to(package)}:{line}: {module}"
        for source in sorted(package.rglob("*.py"))
        for line, module in _imports(source)
        if module == "sidekick_usages.providers"
        or module.startswith("sidekick_usages.providers.")
    ]

    assert violations == []


def test_provider_boundary_schemas_are_the_only_validation_owners() -> None:
    """Pydantic remains confined to each provider boundary schema."""
    package = (
        Path(__file__).parents[1] / "src" / "sidekick_usages" / "providers"
    )
    owners = [
        source.relative_to(package)
        for provider_id in ("claude", "codex")
        for source in sorted((package / provider_id).rglob("*.py"))
        if any(
            module == "pydantic" or module.startswith("pydantic.")
            for _, module in _imports(source)
        )
    ]

    assert owners == [Path("claude/schemas.py"), Path("codex/schemas.py")]


def test_provider_packages_keep_composition_and_presentation_outside() -> None:
    """Provider adapters stay independent of CLI and renderer frameworks."""
    package = (
        Path(__file__).parents[1] / "src" / "sidekick_usages" / "providers"
    )
    forbidden = {
        "rich",
        "typer",
        "urllib3",
        "sidekick_usages.cli",
        "sidekick_usages.render",
    }
    violations = [
        f"{source.relative_to(package)}:{line}: {module}"
        for provider_id in ("claude", "codex")
        for source in sorted((package / provider_id).rglob("*.py"))
        for line, module in _imports(source)
        if any(
            module == boundary or module.startswith(f"{boundary}.")
            for boundary in forbidden
        )
    ]

    assert violations == []


def test_codex_credential_bridge_is_the_only_provider_specific_owner() -> None:
    """Provider-specific credential coordination stays in one bridge."""
    package = (
        Path(__file__).parents[1] / "src" / "sidekick_usages" / "credentials"
    )
    owners = [
        source.relative_to(package)
        for source in sorted(package.rglob("*.py"))
        if any(
            module == "sidekick_usages.providers.codex"
            or module.startswith("sidekick_usages.providers.codex.")
            for _, module in _imports(source)
        )
    ]

    assert owners == [Path("codex.py")]


def test_cli_command_surface_is_registered_once_by_focused_owners() -> None:
    """Application assembly exposes the complete intentional command tree."""
    root = get_command(create_app())
    assert isinstance(root, click.Group)
    assert set(root.commands) == {
        "add",
        "check",
        "check-update",
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
        "daemon": {"install", "status", "uninstall"},
        "heartbeat": {"disable", "enable", "run-label", "status"},
        "migrate": {"accounts", "prepare-rollback"},
        "permissions": {"repair"},
    }


def test_command_owners_never_import_the_global_application() -> None:
    """Command registration stays directed toward an injected Typer app."""
    commands = (
        Path(__file__).parents[1]
        / "src"
        / "sidekick_usages"
        / "cli"
        / "commands"
    )
    violations: list[str] = []
    for source in sorted(commands.glob("*.py")):
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and (
                    node.module == "sidekick_usages.cli.app"
                    or (
                        node.module == "sidekick_usages.cli"
                        and any(alias.name == "app" for alias in node.names)
                    )
                )
            ) or (
                isinstance(node, ast.Import)
                and any(
                    alias.name == "sidekick_usages.cli.app"
                    for alias in node.names
                )
            ):
                violations.append(f"{source.name}:{node.lineno}")

    assert violations == []

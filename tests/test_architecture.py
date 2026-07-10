"""Load-bearing source dependency boundaries."""

import ast
from collections.abc import Iterator
from pathlib import Path


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

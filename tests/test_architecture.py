"""Load-bearing source dependency boundaries."""

import ast
from collections.abc import Iterator
from pathlib import Path


def _imports(source: Path) -> Iterator[tuple[int, str]]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from ((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            yield node.lineno, node.module


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

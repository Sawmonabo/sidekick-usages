"""Repository-wide Python module hygiene checks."""

import ast
import re
from collections.abc import Sequence

from architecture.models import ArchitectureFinding, SourceUnit
from architecture.source import dotted_name, finding

type _Declaration = ast.Assign | ast.AnnAssign | ast.TypeAlias

_SUPPRESSION = re.compile(
    r"#\s*(?:noqa(?:\s*:)?|type:\s*ignore|nosec)(?:\b|$)",
    re.IGNORECASE,
)


def check_hygiene(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Enforce Python conventions across source, tests, and packaging."""
    for unit in units:
        _check_suppressions(unit, violations)
        scoped_imports = _scoped_imports(unit.tree)
        for node in ast.walk(unit.tree):
            violation = _node_violation(unit, node, scoped_imports)
            if violation is not None:
                violations.append(violation)
        for node in _late_module_declarations(unit.tree):
            violations.append(
                finding(
                    unit,
                    node,
                    "HYG007",
                    "module declarations must precede functions and classes",
                )
            )


def _check_suppressions(
    unit: SourceUnit,
    violations: list[ArchitectureFinding],
) -> None:
    for number, line in enumerate(unit.source.splitlines(), start=1):
        if _SUPPRESSION.search(line) is not None:
            violations.append(
                ArchitectureFinding(
                    unit.path,
                    number,
                    "HYG003",
                    "source suppression requires architecture approval",
                )
            )


def _scoped_imports(tree: ast.Module) -> frozenset[int]:
    return frozenset(
        id(descendant)
        for scope in ast.walk(tree)
        if isinstance(
            scope,
            (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef),
        )
        for descendant in ast.walk(scope)
        if isinstance(descendant, (ast.Import, ast.ImportFrom))
    )


def _node_violation(
    unit: SourceUnit,
    node: ast.AST,
    scoped_imports: frozenset[int],
) -> ArchitectureFinding | None:
    if _is_any(node) or (
        isinstance(node, ast.Call)
        and dotted_name(node.func) in {"cast", "typing.cast"}
    ):
        return finding(unit, node, "HYG001", "Any and cast are forbidden")
    if (
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
    ):
        return finding(
            unit,
            node,
            "HYG002",
            "legacy future annotations import is forbidden",
        )
    if isinstance(node, ast.ExceptHandler) and _silently_broad(node):
        return finding(
            unit,
            node,
            "HYG004",
            "broad exception handler silently discards a failure",
        )
    if (
        isinstance(node, (ast.Import, ast.ImportFrom))
        and id(node) in scoped_imports
    ) or (
        isinstance(node, ast.Call)
        and dotted_name(node.func) in {"__import__", "importlib.import_module"}
    ):
        return finding(
            unit,
            node,
            "HYG005",
            "imports must be static and outside functions/classes",
        )
    if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
        alias.asname is not None for alias in node.names
    ):
        return finding(unit, node, "HYG006", "import aliases are forbidden")
    return None


def _is_any(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == "Any") or (
        isinstance(node, ast.Attribute)
        and node.attr == "Any"
        and isinstance(node.value, ast.Name)
        and node.value.id == "typing"
    )


def _silently_broad(handler: ast.ExceptHandler) -> bool:
    broad = handler.type is None or dotted_name(handler.type) in {
        "BaseException",
        "Exception",
    }
    return (
        broad
        and bool(handler.body)
        and all(
            isinstance(statement, (ast.Continue, ast.Pass))
            for statement in handler.body
        )
    )


def _late_module_declarations(tree: ast.Module) -> tuple[_Declaration, ...]:
    nodes = sorted(
        _module_scope_nodes(tree.body),
        key=lambda node: node.lineno,
    )
    first_behavior = next(
        (
            node.lineno
            for node in nodes
            if isinstance(
                node,
                (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef),
            )
        ),
        None,
    )
    if first_behavior is None:
        return ()
    return tuple(
        node
        for node in nodes
        if node.lineno > first_behavior
        and isinstance(node, (ast.AnnAssign, ast.Assign, ast.TypeAlias))
    )


def _module_scope_nodes(
    statements: Sequence[ast.stmt],
) -> tuple[ast.stmt, ...]:
    nodes: list[ast.stmt] = []
    for node in statements:
        nodes.append(node)
        if isinstance(
            node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef)
        ):
            continue
        if isinstance(node, (ast.AsyncFor, ast.For, ast.If, ast.While)):
            nodes.extend(_module_scope_nodes((*node.body, *node.orelse)))
        elif isinstance(node, (ast.Try, ast.TryStar)):
            nested = [*node.body, *node.orelse, *node.finalbody]
            for handler in node.handlers:
                nested.extend(handler.body)
            nodes.extend(_module_scope_nodes(nested))
        elif isinstance(node, (ast.AsyncWith, ast.With)):
            nodes.extend(_module_scope_nodes(node.body))
        elif isinstance(node, ast.Match):
            for case in node.cases:
                nodes.extend(_module_scope_nodes(case.body))
    return tuple(nodes)

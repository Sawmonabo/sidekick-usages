"""Repository package and namespace shape checks."""

import ast
from collections.abc import Sequence
from pathlib import PurePosixPath

from architecture.models import ArchitectureFinding, SourceUnit
from architecture.source import STALE_SOURCE_FILES, finding

MIN_NAMESPACE_FAMILY_SIZE = 2
_OWNER_MODULE_TOKENS = frozenset(
    {
        "error",
        "errors",
        "model",
        "models",
        "port",
        "ports",
        "schema",
        "schemas",
        "type",
        "types",
    }
)
_ROOT_INITIALIZER = PurePosixPath("src/sidekick_usages/__init__.py")


def check_source_shape(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Enforce clean package initializers and cohesive file namespaces."""
    present = {str(unit.path) for unit in units}
    stale = sorted(STALE_SOURCE_FILES & present)
    if stale:
        violations.append(
            ArchitectureFinding(
                PurePosixPath("src/sidekick_usages"),
                1,
                "PKG001",
                f"stale converted modules remain: {stale}",
            )
        )
    _check_private_package_names(units, violations)
    _check_flat_namespaces(units, violations)
    _check_owner_module_names(units, violations)
    for unit in units:
        if not _repository_code(unit) or unit.path.name != "__init__.py":
            continue
        invalid = next(
            (
                node
                for index, node in enumerate(unit.tree.body)
                if not _initializer_statement_allowed(unit.path, index, node)
            ),
            None,
        )
        if invalid is not None:
            violations.append(
                finding(unit, invalid, "PKG001", "initializer is not thin")
            )


def _check_private_package_names(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    for unit in units:
        if not _repository_code(unit):
            continue
        private = next(
            (part for part in unit.path.parent.parts if part.startswith("_")),
            None,
        )
        if private is not None:
            violations.append(
                finding(
                    unit,
                    None,
                    "PKG001",
                    f"package directory {private!r} cannot be private",
                )
            )


def _initializer_statement_allowed(
    path: PurePosixPath,
    index: int,
    node: ast.stmt,
) -> bool:
    if (
        index == 0
        and isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ):
        return True
    return (
        path == _ROOT_INITIALIZER
        and isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "__version__"
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _check_owner_module_names(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    for unit in units:
        if not _repository_code(unit) or unit.path.name == "__init__.py":
            continue
        tokens = frozenset(unit.path.stem.split("_"))
        invalid = unit.path.stem == "contracts" or (
            len(tokens) > 1 and bool(tokens & _OWNER_MODULE_TOKENS)
        )
        if invalid:
            violations.append(
                finding(
                    unit,
                    None,
                    "PKG003",
                    "owner types, models, schemas, ports, and errors "
                    "require designated modules",
                )
            )


def _check_flat_namespaces(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    families: dict[
        tuple[PurePosixPath, str, str],
        list[SourceUnit],
    ] = {}
    for unit in units:
        if not _repository_code(unit) or unit.path.name == "__init__.py":
            continue
        stem = unit.path.stem
        if stem.startswith("__"):
            continue
        tokens = stem.split("_")
        families.setdefault(
            (unit.path.parent, "prefix", tokens[0]),
            [],
        ).append(unit)
        if len(tokens) > 1:
            families.setdefault(
                (unit.path.parent, "suffix", tokens[-1]),
                [],
            ).append(unit)
    for (parent, kind, token), members in sorted(
        families.items(),
        key=lambda item: tuple(str(value) for value in item[0]),
    ):
        if len(members) < MIN_NAMESPACE_FAMILY_SIZE:
            continue
        names = sorted(unit.path.name for unit in members)
        violations.append(
            ArchitectureFinding(
                min(unit.path for unit in members),
                1,
                "PKG002",
                f"flat {kind} family {token!r} in {parent}: {names}",
            )
        )


def _repository_code(unit: SourceUnit) -> bool:
    return unit.production or unit.packaging

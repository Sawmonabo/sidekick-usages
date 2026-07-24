"""Typed AST and source-loading primitives for the architecture gate."""

import ast
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

VIOLATION_RULE_IDS = frozenset(
    {
        "SIZE001",
        "HYG001",
        "HYG002",
        "HYG003",
        "HYG004",
        "HYG005",
        "DEP001",
        "DEP002",
        "DEP003",
        "DEP004",
        "DEP005",
        "DEP006",
        "DEP007",
        "DEP008",
        "PATH001",
        "PATH002",
        "TIME001",
        "TIME002",
        "CFG001",
        "CTX001",
        "CTX002",
        "CLI001",
        "MIG001",
        "HTTP001",
        "BRAND001",
        "PKG001",
        "SCHEMA001",
        "MODEL001",
        "ACT001",
    }
)
WARNING_RULE_IDS = frozenset({"SIZE002"})
STALE_SOURCE_FILES = frozenset(
    {
        "src/sidekick_usages/cli.py",
        "src/sidekick_usages/cli_help.py",
        "src/sidekick_usages/http.py",
        "src/sidekick_usages/lifetime.py",
        "src/sidekick_usages/persistence/migration_errors.py",
        "src/sidekick_usages/persistence/migrations.py",
        "src/sidekick_usages/providers/claude.py",
        "src/sidekick_usages/providers/codex.py",
        "src/sidekick_usages/render.py",
        "src/sidekick_usages/report.py",
        "src/sidekick_usages/store.py",
        "src/sidekick_usages/token_input.py",
        "src/sidekick_usages/timestamps.py",
    }
)
ROBOT_ART = (
    "      o",
    "     .-.",
    "  .--┴-┴--.",
    "  | O   O |",
    "  | ||||| |",
    "  '--___--'",
)


@dataclass(frozen=True, slots=True, order=True)
class ArchitectureFinding:
    """One stable architecture diagnostic."""

    path: PurePosixPath
    line: int
    rule_id: str
    message: str

    def render(self) -> str:
        """Render a terminal-friendly diagnostic."""
        return f"{self.path}:{self.line}: {self.rule_id} {self.message}"


@dataclass(frozen=True, slots=True)
class ArchitectureReport:
    """Complete errors and review warnings for one repository snapshot."""

    violations: tuple[ArchitectureFinding, ...]
    warnings: tuple[ArchitectureFinding, ...]


@dataclass(frozen=True, slots=True)
class SourceUnit:
    """One parsed repository source file."""

    path: PurePosixPath
    source: str
    tree: ast.Module

    @property
    def production(self) -> bool:
        """Return whether the source belongs to the installed package."""
        return self.path.parts[:2] == ("src", "sidekick_usages")


@dataclass(frozen=True, slots=True)
class SourceMutation:
    """One deliberate source change used to prove an architecture rule."""

    rule_id: str
    path: str
    original: str
    replacement: str


def load_units(
    root: Path,
    overrides: Mapping[PurePosixPath, str],
) -> tuple[SourceUnit, ...]:
    """Load source and test modules with optional in-memory replacements."""
    remaining = dict(overrides)
    units: list[SourceUnit] = []
    paths = (*root.joinpath("src").rglob("*.py"),)
    paths += (*root.joinpath("tests").rglob("*.py"),)
    for source_path in sorted(paths):
        relative = PurePosixPath(source_path.relative_to(root).as_posix())
        source = remaining.pop(
            relative,
            source_path.read_text(encoding="utf-8"),
        )
        units.append(source_unit(relative, source))
    units.extend(
        source_unit(path, source) for path, source in remaining.items()
    )
    return tuple(units)


def source_unit(path: PurePosixPath, source: str) -> SourceUnit:
    """Parse one source unit."""
    return SourceUnit(path, source, ast.parse(source, filename=str(path)))


def finding(
    unit: SourceUnit,
    node: ast.AST | None,
    rule_id: str,
    message: str,
) -> ArchitectureFinding:
    """Create a finding at a node or at the start of its source file."""
    return ArchitectureFinding(
        unit.path,
        1 if node is None else getattr(node, "lineno", 1),
        rule_id,
        message,
    )


def scan_imports(unit: SourceUnit) -> Iterable[tuple[ast.AST, str]]:
    """Yield resolved import candidates found in a source unit."""
    for node in ast.walk(unit.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node, alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _import_from_base(unit, node)
            for alias in node.names:
                if alias.name == "*":
                    if base:
                        yield node, base
                    continue
                yield (
                    node,
                    ".".join(part for part in (base, alias.name) if part),
                )


def _import_from_base(unit: SourceUnit, node: ast.ImportFrom) -> str:
    """Resolve one ``from`` import base against its importing package."""
    if node.level == 0:
        return node.module or ""
    parts = unit.path.parts
    if parts and parts[0] == "src":
        parts = parts[1:]
    package = parts[:-1]
    if unit.path.name == "__init__.py":
        package = parts[:-1]
    ascend = node.level - 1
    if ascend:
        package = package[:-ascend] if ascend <= len(package) else ()
    module = tuple((node.module or "").split(".")) if node.module else ()
    return ".".join((*package, *module))


def matches(module: str, boundary: str) -> bool:
    """Return whether a module is at or below an import boundary."""
    return module == boundary or module.startswith(f"{boundary}.")


def matches_any(module: str, boundaries: Iterable[str]) -> bool:
    """Return whether a module is below any supplied boundary."""
    return any(matches(module, boundary) for boundary in boundaries)


def dotted_name(node: ast.AST) -> str:
    """Return a dotted name for simple name and attribute nodes."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = dotted_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return "<dynamic>"


def class_node(tree: ast.Module, class_name: str) -> ast.ClassDef | None:
    """Return one top-level class by name."""
    return next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        ),
        None,
    )


def function_node(
    tree: ast.AST,
    function_name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the first function with the supplied name."""
    return next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and node.name == function_name
        ),
        None,
    )


def class_fields(
    tree: ast.Module,
    class_name: str,
) -> tuple[tuple[str, str], ...] | None:
    """Return direct annotated fields from one top-level class."""
    owner = class_node(tree, class_name)
    if owner is None:
        return None
    return tuple(
        (statement.target.id, compact(ast.unparse(statement.annotation)))
        for statement in owner.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
    )


def type_alias(tree: ast.Module, alias_name: str) -> ast.TypeAlias | None:
    """Return one PEP 695 top-level type alias."""
    return next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.TypeAlias)
            and isinstance(node.name, ast.Name)
            and node.name.id == alias_name
        ),
        None,
    )


def enum_values(tree: ast.Module, class_name: str) -> dict[str, str] | None:
    """Return direct string members from one enum class."""
    owner = class_node(tree, class_name)
    if owner is None:
        return None
    return {
        statement.targets[0].id: statement.value.value
        for statement in owner.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    }


def assigned_names(node: ast.Assign | ast.AnnAssign) -> tuple[str, ...]:
    """Return simple target names from one assignment."""
    targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
    return tuple(
        target.id for target in targets if isinstance(target, ast.Name)
    )


def assignment_literal(tree: ast.Module, target_name: str) -> object | None:
    """Evaluate one top-level literal assignment when present."""
    for node in tree.body:
        if isinstance(
            node, (ast.AnnAssign, ast.Assign)
        ) and target_name in assigned_names(node):
            if node.value is None:
                return None
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return None
    return None


def contains_call(node: ast.AST, call_name: str) -> bool:
    """Return whether a node contains a call to a simple dotted name."""
    return any(
        isinstance(child, ast.Call)
        and dotted_name(child.func) == call_name
        for child in ast.walk(node)
    )


def contains_string(node: ast.AST, value: str) -> bool:
    """Return whether a node contains an exact string literal."""
    return any(
        isinstance(child, ast.Constant) and child.value == value
        for child in ast.walk(node)
    )


def compact(value: str) -> str:
    """Remove insignificant whitespace from an unparsed annotation."""
    return "".join(value.split())

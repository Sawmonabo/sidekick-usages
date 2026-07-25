"""Typed AST and source-loading primitives for the architecture gate."""

import ast
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

from architecture.models import ArchitectureFinding, SourceUnit

VIOLATION_RULE_IDS = frozenset(
    {
        "SIZE001",
        "HYG001",
        "HYG002",
        "HYG003",
        "HYG004",
        "HYG005",
        "HYG006",
        "HYG007",
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
        "HTTP001",
        "BRAND001",
        "PKG001",
        "PKG002",
        "PKG003",
        "SCHEMA001",
        "ACT001",
    }
)
WARNING_RULE_IDS = frozenset({"SIZE002"})
STALE_SOURCE_FILES = frozenset(
    {
        "src/sidekick_usages/cli.py",
        "src/sidekick_usages/cli_help.py",
        "src/sidekick_usages/credentials/claude_lifetime.py",
        "src/sidekick_usages/credentials/claude_setup_save.py",
        "src/sidekick_usages/credentials/claude_transitions.py",
        "src/sidekick_usages/credentials/codex.py",
        "src/sidekick_usages/credentials/codex/authorities.py",
        "src/sidekick_usages/daemon/client.py",
        "src/sidekick_usages/daemon/control.py",
        "src/sidekick_usages/daemon/diagnostics.py",
        "src/sidekick_usages/daemon/dispatch.py",
        "src/sidekick_usages/daemon/entrypoint.py",
        "src/sidekick_usages/daemon/peer.py",
        "src/sidekick_usages/daemon/protocol.py",
        "src/sidekick_usages/daemon/recovery.py",
        "src/sidekick_usages/daemon/runtime/callbacks.py",
        "src/sidekick_usages/daemon/runtime/entrypoint.py",
        "src/sidekick_usages/daemon/scheduler.py",
        "src/sidekick_usages/daemon/supervisor.py",
        "src/sidekick_usages/daemon/worker_entrypoint.py",
        "src/sidekick_usages/daemon/worker/entrypoint.py",
        "src/sidekick_usages/daemon/worker_runtime.py",
        "src/sidekick_usages/daemon/workers.py",
        "src/sidekick_usages/doctor.py",
        "src/sidekick_usages/doctor_credentials.py",
        "src/sidekick_usages/http.py",
        "src/sidekick_usages/lifetime.py",
        "src/sidekick_usages/persistence/_platform/__init__.py",
        "src/sidekick_usages/persistence/_platform/macos.py",
        "src/sidekick_usages/persistence/_platform/macos_acl.py",
        "src/sidekick_usages/persistence/_platform/posix.py",
        "src/sidekick_usages/persistence/_platform/posix_files.py",
        "src/sidekick_usages/persistence/_platform/posix_mounts.py",
        "src/sidekick_usages/persistence/_platform/posix_namespace.py",
        "src/sidekick_usages/persistence/_platform/posix_private.py",
        "src/sidekick_usages/persistence/_platform/posix_private_bundles.py",
        "src/sidekick_usages/persistence/_platform/posix_private_platform.py",
        "src/sidekick_usages/persistence/_platform/posix_provider_stage.py",
        "src/sidekick_usages/persistence/_platform/windows.py",
        "src/sidekick_usages/persistence/_platform/windows_files.py",
        "src/sidekick_usages/persistence/_platform/windows_handles.py",
        "src/sidekick_usages/persistence/_platform/windows_namespace.py",
        "src/sidekick_usages/persistence/_platform/windows_private.py",
        "src/sidekick_usages/persistence/_platform/windows_private_bundles.py",
        "src/sidekick_usages/persistence/_platform/windows_private_tree.py",
        "src/sidekick_usages/persistence/_platform/windows_security.py",
        "src/sidekick_usages/persistence/account_index.py",
        "src/sidekick_usages/persistence/account_runtime_bridge.py",
        "src/sidekick_usages/persistence/account_store.py",
        "src/sidekick_usages/persistence/activation_journal.py",
        "src/sidekick_usages/persistence/credential_ownership.py",
        "src/sidekick_usages/persistence/credential_refresh.py",
        "src/sidekick_usages/persistence/credential_refresh_artifacts.py",
        "src/sidekick_usages/persistence/credential_refresh_merge.py",
        "src/sidekick_usages/persistence/credential_repository.py",
        "src/sidekick_usages/persistence/credential_transaction_plans.py",
        "src/sidekick_usages/persistence/credential_transaction_recovery.py",
        "src/sidekick_usages/persistence/credential_transactions.py",
        "src/sidekick_usages/persistence/filesystem.py",
        "src/sidekick_usages/persistence/filesystem_access.py",
        "src/sidekick_usages/persistence/operation_authority.py",
        "src/sidekick_usages/persistence/operation_queue.py",
        "src/sidekick_usages/persistence/private_bundle_paths.py",
        "src/sidekick_usages/persistence/private_bundle_references.py",
        "src/sidekick_usages/persistence/private_bundle_writes.py",
        "src/sidekick_usages/persistence/private_credential_contracts.py",
        "src/sidekick_usages/persistence/private_credentials.py",
        "src/sidekick_usages/persistence/private_filesystem.py",
        "src/sidekick_usages/persistence/private/contracts.py",
        "src/sidekick_usages/persistence/platform/contracts.py",
        "src/sidekick_usages/persistence/schema/private_refresh.py",
        "src/sidekick_usages/persistence/schema/refresh.py",
        "src/sidekick_usages/persistence/schema/refresh_stage.py",
        "src/sidekick_usages/persistence/selected_state.py",
        "src/sidekick_usages/persistence/service_state.py",
        "src/sidekick_usages/persistence/state_fields.py",
        "src/sidekick_usages/persistence/state_files.py",
        "src/sidekick_usages/persistence/state_filesystem.py",
        "src/sidekick_usages/persistence/state_json.py",
        "src/sidekick_usages/persistence/state_validation.py",
        "src/sidekick_usages/persistence/transaction.py",
        "src/sidekick_usages/persistence/worker_results.py",
        "src/sidekick_usages/providers/claude.py",
        "src/sidekick_usages/providers/claude/credential_schemas.py",
        "src/sidekick_usages/providers/claude/schemas.py",
        "src/sidekick_usages/providers/codex.py",
        "src/sidekick_usages/providers/codex/account.py",
        "src/sidekick_usages/providers/codex/app_server.py",
        "src/sidekick_usages/providers/codex/capabilities.py",
        "src/sidekick_usages/providers/codex/errors.py",
        "src/sidekick_usages/providers/codex/executable.py",
        "src/sidekick_usages/providers/codex/jsonrpc.py",
        "src/sidekick_usages/providers/codex/models/app_server.py",
        "src/sidekick_usages/providers/codex/process.py",
        "src/sidekick_usages/providers/codex/broker/external_auth.py",
        "src/sidekick_usages/providers/codex/types/app_server.py",
        "src/sidekick_usages/render.py",
        "src/sidekick_usages/report.py",
        "src/sidekick_usages/store.py",
        "src/sidekick_usages/token_input.py",
        "src/sidekick_usages/timestamps.py",
        "src/sidekick_usages/usage/activity_render.py",
        "src/sidekick_usages/usage/narrow_render.py",
        "src/sidekick_usages/usage/render.py",
        "src/sidekick_usages/usage/reset_display.py",
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


def load_units(
    root: Path,
    overrides: Mapping[PurePosixPath, str],
) -> tuple[SourceUnit, ...]:
    """Load source and test modules with optional in-memory replacements."""
    remaining = dict(overrides)
    units: list[SourceUnit] = []
    paths = (*root.joinpath("src").rglob("*.py"),)
    paths += (*root.joinpath("tests").rglob("*.py"),)
    paths += (*root.joinpath("packaging").rglob("*.py"),)
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
        isinstance(child, ast.Call) and dotted_name(child.func) == call_name
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

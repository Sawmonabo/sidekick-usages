"""Runtime time, settings, and provider-activity contracts."""

import ast
from collections.abc import Sequence

from architecture.models import ArchitectureFinding, SourceUnit
from architecture.source import (
    dotted_name,
    finding,
    matches,
    matches_any,
    scan_imports,
)

_REQUIRED_ACTIVITY_FILES = frozenset(
    {
        "src/sidekick_usages/persistence/snapshots/activity.py",
        "src/sidekick_usages/providers/claude/activity.py",
        "src/sidekick_usages/providers/codex/activity.py",
    }
)


def check_time_and_settings(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Enforce explicit clock ownership and reject global settings."""
    for unit in units:
        if unit.production:
            _check_time_unit(unit, violations)


def _check_time_unit(
    unit: SourceUnit,
    violations: list[ArchitectureFinding],
) -> None:
    path = str(unit.path)
    if unit.path.name in {"settings.py", "configuration.py"}:
        violations.append(
            finding(unit, None, "CFG001", "global settings are not approved")
        )
    if unit.path.name == "timestamps.py":
        violations.append(
            finding(unit, None, "TIME002", "timestamps.py is forbidden")
        )
    settings_class = next(
        (
            node
            for node in ast.walk(unit.tree)
            if isinstance(node, ast.ClassDef)
            and any(
                dotted_name(base).endswith("BaseSettings")
                for base in node.bases
            )
        ),
        None,
    )
    if settings_class is not None:
        violations.append(
            finding(unit, settings_class, "CFG001", "global settings model")
        )
    for node, module in scan_imports(unit):
        if matches(module, "pydantic_settings"):
            violations.append(
                finding(unit, node, "CFG001", "pydantic-settings is deferred")
            )
        if path.endswith("/core/expiry.py") and matches_any(
            module, ("sidekick_usages.clock", "time")
        ):
            violations.append(
                finding(unit, node, "TIME002", "expiry cannot acquire time")
            )
    current_time = next(
        (
            node
            for node in ast.walk(unit.tree)
            if isinstance(node, ast.Call)
            and dotted_name(node.func)
            in {"datetime.now", "datetime.utcnow", "time.time"}
        ),
        None,
    )
    if current_time is not None and not path.endswith("/clock.py"):
        violations.append(
            finding(unit, current_time, "TIME001", "use the Clock boundary")
        )
    if path.endswith("/core/time.py") and not _pure_time_module(unit.tree):
        violations.append(
            finding(
                unit,
                None,
                "TIME002",
                "core/time.py must contain only pure as_utc normalization",
            )
        )


def _pure_time_module(tree: ast.Module) -> bool:
    functions = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    ]
    forbidden = {"fromisoformat", "isoformat", "now", "strftime", "strptime"}
    return functions == ["as_utc"] and not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in forbidden
        for node in ast.walk(tree)
    )


def check_activity_contract(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Enforce provider-owned activity and prohibit the old fallback."""
    production = tuple(unit for unit in units if unit.production)
    present = {str(unit.path) for unit in production}
    missing = sorted(_REQUIRED_ACTIVITY_FILES - present)
    invalid: list[tuple[SourceUnit, ast.AST | None]] = []
    for unit in production:
        path = str(unit.path)
        if path == "src/sidekick_usages/lifetime.py":
            invalid.append((unit, None))
        invalid.extend(
            (unit, node)
            for node, module in scan_imports(unit)
            if matches(module, "sidekick_usages.lifetime")
        )
        if path == "src/sidekick_usages/providers/codex/activity.py":
            invalid.extend(
                (unit, node)
                for node, module in scan_imports(unit)
                if matches_any(module, ("glob", "os", "pathlib", "sqlite3"))
            )
            if any(
                term in unit.source.lower()
                for term in ("codex-lifetime-cache", "rollout")
            ):
                invalid.append((unit, None))
        if (
            path == "src/sidekick_usages/usage/presentation/activity.py"
            and any(
                term in unit.source for term in ("known tokens", "local CLI")
            )
        ):
            invalid.append((unit, None))
    if missing or invalid:
        unit, node = (
            invalid[0]
            if invalid
            else (
                next(iter(production)),
                None,
            )
        )
        violations.append(
            finding(
                unit,
                node,
                "ACT001",
                "provider activity ownership or no-fallback contract changed",
            )
        )

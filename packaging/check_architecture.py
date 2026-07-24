#!/usr/bin/env python3
"""Enforce Sidekick's repository-specific architecture contracts."""

import ast
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from architecture_ast import (
    STALE_SOURCE_FILES,
    ArchitectureFinding,
    ArchitectureReport,
    SourceUnit,
    finding,
    function_node,
    load_units,
    matches,
    matches_any,
)
from architecture_ast import (
    imports as scan_imports,
)
from architecture_ast import (
    name as dotted_name,
)
from architecture_ownership import check_ownership
from architecture_value_contracts import check_value_contracts

REPO_ROOT = Path(__file__).resolve().parents[1]
MAX_MODULE_LINES = 1000
REVIEW_MODULE_LINES = 800
MAX_CLI_APP_LINES = 200

_SERVICE_FILES = frozenset(
    {
        "src/sidekick_usages/credentials/service.py",
        "src/sidekick_usages/heartbeat/service.py",
        "src/sidekick_usages/maintenance.py",
        "src/sidekick_usages/update.py",
        "src/sidekick_usages/usage/activity.py",
        "src/sidekick_usages/usage/service.py",
    }
)
_RENDERER_FILES = frozenset(
    {
        "src/sidekick_usages/branding.py",
        "src/sidekick_usages/heartbeat/render.py",
        "src/sidekick_usages/usage/activity_render.py",
        "src/sidekick_usages/usage/narrow_render.py",
        "src/sidekick_usages/usage/render.py",
        "src/sidekick_usages/usage/reset_display.py",
    }
)
_CREDENTIAL_LEASE_CONSUMERS = frozenset(
    {
        "src/sidekick_usages/cli/context.py",
        "src/sidekick_usages/credentials/authorities.py",
        "src/sidekick_usages/credentials/refresh.py",
        "src/sidekick_usages/heartbeat/service.py",
        "src/sidekick_usages/usage/activity.py",
        "src/sidekick_usages/usage/service.py",
    }
)
_PYDANTIC_OWNERS = frozenset(
    {
        "src/sidekick_usages/persistence/account_schema_v3.py",
        "src/sidekick_usages/persistence/_schema_models.py",
        "src/sidekick_usages/persistence/activity_snapshots.py",
        "src/sidekick_usages/persistence/credential_authorities.py",
        "src/sidekick_usages/persistence/credential_transaction_schema.py",
        "src/sidekick_usages/persistence/credential_refresh_private_stage.py",
        "src/sidekick_usages/persistence/credential_refresh_schema.py",
        "src/sidekick_usages/persistence/credential_refresh_stage.py",
        "src/sidekick_usages/persistence/selection_schema.py",
        "src/sidekick_usages/persistence/schemas.py",
        "src/sidekick_usages/providers/claude/credential_schemas.py",
        "src/sidekick_usages/providers/claude/schemas.py",
        "src/sidekick_usages/providers/codex/schemas.py",
        "src/sidekick_usages/serialization/json.py",
    }
)
_PROVIDER_PERSISTENCE_IMPORTS = frozenset(
    {
        "sidekick_usages.persistence.artifacts",
        "sidekick_usages.persistence.migrations.ports",
        "sidekick_usages.persistence.private_credentials",
    }
)
_TRANSPORT_ROOTS = frozenset({"httpx", "requests", "tenacity", "urllib3"})
_SUPPRESSION = re.compile(
    r"#\s*(?:noqa(?:\s*:)?|type:\s*ignore|nosec)(?:\b|$)",
    re.IGNORECASE,
)


def check_repository(
    root: Path = REPO_ROOT,
    *,
    source_overrides: Mapping[str, str] | None = None,
    pyproject_override: str | None = None,
) -> ArchitectureReport:
    """Check the repository with optional in-memory test mutations."""
    overrides = {
        PurePosixPath(path): source
        for path, source in (source_overrides or {}).items()
    }
    units = load_units(root, overrides)

    violations: list[ArchitectureFinding] = []
    warnings: list[ArchitectureFinding] = []
    _check_sizes(units, violations, warnings)
    _check_hygiene(units, violations)
    _check_import_boundaries(units, violations)
    _check_time_and_settings(units, violations)
    check_value_contracts(units, violations)
    _check_activity_contract(units, violations)
    _check_cli(units, violations)
    check_ownership(units, violations)
    _check_source_shape(units, violations)
    project_text = pyproject_override or root.joinpath(
        "pyproject.toml"
    ).read_text(encoding="utf-8")
    _check_project(project_text, violations)
    return ArchitectureReport(
        tuple(sorted(set(violations))),
        tuple(sorted(set(warnings))),
    )


def _check_sizes(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
    warnings: list[ArchitectureFinding],
) -> None:
    for unit in units:
        lines = len(unit.source.splitlines())
        if lines > MAX_MODULE_LINES:
            violations.append(
                finding(
                    unit,
                    None,
                    "SIZE001",
                    f"module has {lines} lines; limit is {MAX_MODULE_LINES}",
                )
            )
        elif lines >= REVIEW_MODULE_LINES:
            warnings.append(
                finding(
                    unit,
                    None,
                    "SIZE002",
                    f"module has {lines} lines; review cohesion",
                )
            )


def _check_hygiene(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    for unit in units:
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
        for node in ast.walk(unit.tree):
            if _is_any(node) or (
                isinstance(node, ast.Call)
                and dotted_name(node.func) in {"cast", "typing.cast"}
            ):
                violations.append(
                    finding(unit, node, "HYG001", "Any and cast are forbidden")
                )
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module == "__future__"
                and any(alias.name == "annotations" for alias in node.names)
            ):
                violations.append(
                    finding(
                        unit,
                        node,
                        "HYG002",
                        "legacy future annotations import is forbidden",
                    )
                )
            elif isinstance(node, ast.ExceptHandler) and _silently_broad(node):
                violations.append(
                    finding(
                        unit,
                        node,
                        "HYG004",
                        "broad exception handler silently discards a failure",
                    )
                )


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


def _check_import_boundaries(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    for unit in units:
        if not unit.production:
            continue
        path = str(unit.path)
        unit_imports = tuple(scan_imports(unit))
        for node, module in unit_imports:
            _check_import(unit, path, node, module, violations)
        if path in _RENDERER_FILES:
            _check_renderer(unit, unit_imports, violations)


def _check_import(
    unit: SourceUnit,
    path: str,
    node: ast.AST,
    module: str,
    violations: list[ArchitectureFinding],
) -> None:
    root = module.split(".", maxsplit=1)[0]
    checks = (
        (
            "DEP001",
            "/core/" in path,
            (
                matches_any(
                    module,
                    (
                        "click",
                        "os",
                        "pathlib",
                        "platformdirs",
                        "rich",
                        "shutil",
                        "subprocess",
                        "typer",
                        "urllib3",
                    ),
                )
                or (
                    matches(module, "sidekick_usages")
                    and not matches(module, "sidekick_usages.core")
                )
            ),
            "core cannot import infrastructure or application owners",
        ),
        (
            "DEP002",
            "/cli/" not in path and not path.endswith("/__main__.py"),
            matches(module, "sidekick_usages.cli"),
            "non-CLI code cannot import CLI composition",
        ),
        (
            "DEP003",
            "/persistence/" in path,
            matches_any(
                module,
                (
                    "sidekick_usages.credentials",
                    "sidekick_usages.providers",
                ),
            ),
            "persistence cannot import credentials or providers",
        ),
        (
            "DEP005",
            "/http/" in path,
            matches_any(
                module,
                (
                    "click",
                    "rich",
                    "typer",
                    "sidekick_usages.cli",
                    "sidekick_usages.persistence",
                    "sidekick_usages.providers",
                    "sidekick_usages.usage",
                ),
            ),
            "HTTP cannot import application adapters",
        ),
        (
            "DEP006",
            path in _SERVICE_FILES or "/credentials/" in path,
            (
                matches_any(module, ("click", "rich", "typer"))
                or (
                    "/credentials/" in path
                    and matches_any(
                        module,
                        (
                            "sidekick_usages.cli",
                            "sidekick_usages.daemon",
                            "sidekick_usages.doctor",
                            "sidekick_usages.heartbeat",
                            "sidekick_usages.maintenance",
                            "sidekick_usages.update",
                            "sidekick_usages.usage",
                        ),
                    )
                )
            ),
            "services and credential workflows cannot import presentation "
            "or higher workflows",
        ),
        (
            "DEP007",
            "/http/" not in path,
            root in _TRANSPORT_ROOTS,
            "transport and retry dependencies belong to http/",
        ),
        (
            "DEP008",
            path not in _CREDENTIAL_LEASE_CONSUMERS,
            matches(
                module,
                "sidekick_usages.credentials.authorities",
            ),
            "credential leases are private to worker service boundaries",
        ),
        (
            "PATH001",
            not path.endswith("/paths.py"),
            matches(module, "platformdirs"),
            "platformdirs is private to paths.py",
        ),
        (
            "SCHEMA001",
            path not in _PYDANTIC_OWNERS,
            root == "pydantic",
            "Pydantic belongs only to untrusted boundary schemas",
        ),
    )
    for rule_id, owner, forbidden, message in checks:
        if owner and forbidden:
            violations.append(finding(unit, node, rule_id, message))
    if "/providers/" in path:
        forbidden_provider = matches_any(
            module,
            (
                "rich",
                "typer",
                "urllib3",
                "sidekick_usages.cli",
                "sidekick_usages.credentials",
                "sidekick_usages.heartbeat.service",
                "sidekick_usages.usage",
            ),
        )
        persistence_leak = matches(module, "sidekick_usages.persistence") and (
            not path.endswith("/providers/codex/auth_migration.py")
            or not matches_any(module, _PROVIDER_PERSISTENCE_IMPORTS)
        )
        if forbidden_provider or persistence_leak:
            violations.append(
                finding(
                    unit,
                    node,
                    "DEP004",
                    "provider adapter crosses an unapproved boundary",
                )
            )


def _check_renderer(
    unit: SourceUnit,
    imports: Sequence[tuple[ast.AST, str]],
    violations: list[ArchitectureFinding],
) -> None:
    forbidden = (
        "os",
        "pathlib",
        "subprocess",
        "urllib",
        "sidekick_usages.clock",
        "sidekick_usages.http",
        "sidekick_usages.persistence",
        "sidekick_usages.providers",
    )
    for node, module in imports:
        if matches_any(module, forbidden):
            violations.append(
                finding(
                    unit,
                    node,
                    "DEP006",
                    "renderer cannot import acquisition boundaries",
                )
            )
    printed = next(
        (
            node
            for node in ast.walk(unit.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "print"
        ),
        None,
    )
    if printed is not None:
        violations.append(
            finding(
                unit,
                printed,
                "DEP006",
                "renderer must return values instead of printing",
            )
        )


def _check_time_and_settings(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
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


def _check_activity_contract(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Enforce provider-owned activity and prohibit the old fallback."""
    required = {
        "src/sidekick_usages/persistence/activity_snapshots.py",
        "src/sidekick_usages/providers/claude/activity.py",
        "src/sidekick_usages/providers/codex/activity.py",
    }
    production = tuple(unit for unit in units if unit.production)
    present = {str(unit.path) for unit in production}
    missing = sorted(required - present)
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
        if path == "src/sidekick_usages/usage/activity_render.py" and any(
            term in unit.source for term in ("known tokens", "local CLI")
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


def _check_cli(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    by_path = {str(unit.path): unit for unit in units}
    app = by_path.get("src/sidekick_usages/cli/app.py")
    if app is not None:
        _check_create_app(app, violations)
    accessors = {
        "accounts.py": {"require_app", "require_persistence"},
        "claude.py": {"require_app"},
        "codex.py": {"require_app"},
        "credentials.py": {"require_app"},
        "daemon.py": {"require_daemon"},
        "doctor.py": {"require_doctor"},
        "heartbeat.py": {"require_app"},
        "maintenance.py": {"require_app"},
        "migrate.py": {"require_persistence"},
        "permissions.py": {"require_persistence"},
        "updates.py": {"require_update"},
        "usage.py": {"require_app"},
    }
    for filename, expected in accessors.items():
        command = by_path.get(f"src/sidekick_usages/cli/commands/{filename}")
        if command is not None:
            _check_command_context(command, expected, violations)
    help_unit = by_path.get("src/sidekick_usages/cli/help.py")
    if help_unit is not None:
        forbidden = (
            "sidekick_usages.cli.context",
            "sidekick_usages.clock",
            "sidekick_usages.http",
            "sidekick_usages.paths",
            "sidekick_usages.persistence",
            "sidekick_usages.providers",
        )
        for node, module in scan_imports(help_unit):
            if matches_any(module, forbidden):
                violations.append(
                    finding(help_unit, node, "CLI001", "help composed runtime")
                )


def _check_create_app(
    unit: SourceUnit,
    violations: list[ArchitectureFinding],
) -> None:
    function = function_node(unit.tree, "create_app")
    bad_calls: list[ast.Call] = []
    if function is not None:
        for call in (
            node for node in ast.walk(function) if isinstance(node, ast.Call)
        ):
            name = dotted_name(call.func)
            callback_application = (
                isinstance(call.func, ast.Call)
                and isinstance(call.func.func, ast.Attribute)
                and call.func.func.attr == "callback"
            )
            allowed = name == "typer.Typer" or (
                isinstance(call.func, ast.Attribute)
                and call.func.attr in {"callback", "register"}
            )
            if not allowed and not callback_application:
                bad_calls.append(call)
    if (
        function is None
        or bad_calls
        or len(unit.source.splitlines()) > MAX_CLI_APP_LINES
    ):
        violations.append(
            finding(
                unit,
                bad_calls[0] if bad_calls else function,
                "CLI001",
                "create_app only registers commands and stays below 200 lines",
            )
        )


def _check_command_context(
    unit: SourceUnit,
    expected: set[str],
    violations: list[ArchitectureFinding],
) -> None:
    actual = {
        node.func.attr
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("require_")
    }
    imports_app = any(
        matches(module, "sidekick_usages.cli.app")
        for _, module in scan_imports(unit)
    )
    if actual != expected or imports_app:
        violations.append(
            finding(
                unit,
                None,
                "CLI001",
                "command uses the wrong context or imports the global app",
            )
        )


def _check_source_shape(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
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
    platform_init = "src/sidekick_usages/persistence/_platform/__init__.py"
    for unit in units:
        if (
            not unit.production
            or unit.path.name != "__init__.py"
            or str(unit.path) == platform_init
        ):
            continue
        definition = next(
            (
                node
                for node in unit.tree.body
                if isinstance(
                    node,
                    (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef),
                )
            ),
            None,
        )
        if definition is not None:
            violations.append(
                finding(unit, definition, "PKG001", "initializer is not thin")
            )


def _check_project(
    project_text: str,
    violations: list[ArchitectureFinding],
) -> None:
    project = tomllib.loads(project_text)
    dependencies = project.get("project", {}).get("dependencies", ())
    has_settings = any(
        isinstance(dependency, str)
        and dependency.split(";", maxsplit=1)[0]
        .strip()
        .lower()
        .startswith("pydantic-settings")
        for dependency in dependencies
    )
    if has_settings:
        violations.append(
            ArchitectureFinding(
                PurePosixPath("pyproject.toml"),
                1,
                "CFG001",
                "pydantic-settings requires a separately approved contract",
            )
        )


def main() -> int:
    """Check the current checkout and emit stable diagnostics."""
    report = check_repository()
    for warning in report.warnings:
        sys.stderr.write(f"warning: {warning.render()}\n")
    for violation in report.violations:
        sys.stderr.write(f"error: {violation.render()}\n")
    if report.violations:
        sys.stderr.write(
            "architecture check failed: "
            f"{len(report.violations)} violation(s)\n"
        )
        return 1
    sys.stdout.write(
        "Architecture check passed with "
        f"{len(report.warnings)} cohesion warning(s).\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

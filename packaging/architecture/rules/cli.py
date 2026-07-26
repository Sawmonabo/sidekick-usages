"""CLI composition architecture contracts."""

import ast
from collections.abc import Sequence

from architecture.models import ArchitectureFinding, SourceUnit
from architecture.source import (
    dotted_name,
    finding,
    function_node,
    matches,
    matches_any,
    scan_imports,
)

MAX_CLI_APP_LINES = 200


def check_cli_contract(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Enforce registration-only composition and focused command contexts."""
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
        "permissions.py": {"require_persistence"},
        "updates.py": {"require_update"},
        "usage.py": {"require_app", "require_dashboard"},
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

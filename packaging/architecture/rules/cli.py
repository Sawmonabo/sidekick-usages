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
PUBLIC_BOOTSTRAP_FILE = "src/sidekick_usages/cli/runtime/bootstrap.py"
PUBLIC_BOOTSTRAP_IMPORTS = (
    "collections.abc",
    "os",
    "pathlib",
    "subprocess",
    "sys",
    "sidekick_usages.cli.runtime.routing",
    "sidekick_usages.platform.errors",
    "sidekick_usages.platform.executable",
)
B606_NO_SHELL_CALLS = frozenset(
    {
        "os.execl",
        "os.execle",
        "os.execlp",
        "os.execlpe",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.execvpe",
        "os.spawnl",
        "os.spawnle",
        "os.spawnlp",
        "os.spawnlpe",
        "os.spawnv",
        "os.spawnve",
        "os.spawnvp",
        "os.spawnvpe",
        "os.startfile",
    }
)


def check_cli_contract(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Enforce registration-only composition and focused command contexts."""
    by_path = {str(unit.path): unit for unit in units}
    app = by_path.get("src/sidekick_usages/cli/app.py")
    if app is not None:
        _check_create_app(app, violations)
    _check_public_bootstrap(units, by_path, violations)
    accessors = {
        "accounts.py": {"require_app", "require_persistence"},
        "claude.py": {"require_app"},
        "codex.py": {"require_app"},
        "credentials.py": {"require_app"},
        "daemon.py": {"require_daemon"},
        "doctor.py": {"require_doctor"},
        "heartbeat.py": {"require_app"},
        "maintenance.py": {"require_app"},
        "migration.py": {"require_migration"},
        "permissions.py": {"require_persistence"},
        "updates.py": {"require_update"},
        "usage.py": {"require_app"},
        "use.py": {"require_use"},
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


def _check_public_bootstrap(
    units: Sequence[SourceUnit],
    by_path: dict[str, SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    bootstrap = by_path.get(PUBLIC_BOOTSTRAP_FILE)
    if bootstrap is None:
        return
    for node, module in scan_imports(bootstrap):
        if not any(
            matches(module, allowed)
            for allowed in PUBLIC_BOOTSTRAP_IMPORTS
        ):
            violations.append(
                finding(
                    bootstrap,
                    node,
                    "CLI001",
                    "public bootstrap imported outside its lean boundaries",
                )
            )
    replacements = [
        (unit, node, dotted_name(node.func))
        for unit in units
        if unit.production
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.Call)
        and dotted_name(node.func) in B606_NO_SHELL_CALLS
    ]
    if (
        len(replacements) != 1
        or str(replacements[0][0].path) != PUBLIC_BOOTSTRAP_FILE
        or replacements[0][2] != "os.execve"
    ):
        unit, node = (
            replacements[0][:2] if replacements else (bootstrap, None)
        )
        violations.append(
            finding(
                unit,
                node,
                "CLI001",
                "one qualified bootstrap execve must be the only B606 call",
            )
        )
    qualifications = [
        node
        for node in ast.walk(bootstrap.tree)
        if isinstance(node, ast.Call)
        and dotted_name(node.func) == "qualify_executable"
    ]
    if len(qualifications) != 1:
        violations.append(
            finding(
                bootstrap,
                qualifications[0] if qualifications else None,
                "CLI001",
                "public bootstrap must use the canonical qualifier once",
            )
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

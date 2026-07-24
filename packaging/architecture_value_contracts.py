"""Frozen value-shape contracts for the architecture gate."""

import ast
from collections.abc import Mapping, Sequence

from architecture_ast import (
    ArchitectureFinding,
    SourceUnit,
    class_fields,
    class_node,
    compact,
    dotted_name,
    enum_values,
    finding,
    type_alias,
)


def check_value_contracts(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Enforce frozen path, context, and location value shapes."""
    by_path = {str(unit.path): unit for unit in units}
    paths = by_path.get("src/sidekick_usages/paths.py")
    path_fields = {
        "AccountLocations": (
            ("canonical", "Path"),
            ("existing_sidekick", "Path"),
            ("prototype_cc_usage", "Path"),
        ),
        "PrivateCodexLocations": (
            ("canonical", "Path"),
            ("existing_sidekick", "Path"),
        ),
        "ApplicationPaths": (
            ("accounts", "AccountLocations"),
            ("private_codex", "PrivateCodexLocations"),
            ("activity_snapshots", "Path"),
            ("credential_refresh", "Path"),
            ("private_claude_profiles", "Path"),
            ("credential_authorities", "Path"),
            ("selected_state", "Path"),
            ("activation_journals", "Path"),
            ("durable_operations", "Path"),
            ("service_state", "Path"),
            ("service_logs", "Path"),
            ("runtime_directory", "Path"),
            ("supervisor_socket", "Path"),
            ("supervisor_lock", "Path"),
        ),
    }
    if paths is not None:
        _require_fields(paths, path_fields, "PATH002", violations)

    context = by_path.get("src/sidekick_usages/cli/context.py")
    context_fields = {
        "AppContext": (
            ("accounts", "AccountStore"),
            ("usage", "UsageCheckService"),
            ("credentials", "CredentialService"),
            ("heartbeat", "HeartbeatService"),
            ("maintenance", "TokenMaintenanceService"),
            ("claude_setup_token", "ClaudeSetupToken"),
            (
                "claude_setup_restore",
                "ClaudeSetupTokenRestoreService",
            ),
        ),
        "PersistenceContext": (("persistence", "PersistenceCommands"),),
        "DoctorContext": (("state", "DoctorState"),),
        "DaemonContext": (("daemon", "DaemonManager"),),
        "UpdateContext": (("update", "UpdateService"),),
    }
    if context is not None:
        _require_fields(context, context_fields, "CTX001", violations)
        alias = type_alias(context.tree, "DoctorState")
        if alias is None or compact(ast.unparse(alias.value)) != (
            "DoctorReady|DoctorBlocked|DoctorFailed"
        ):
            violations.append(
                finding(context, alias, "CTX001", "DoctorState is not closed")
            )
        composed = {
            "Composed": (
                ("value", "T"),
                ("_resources", "ExitStack"),
                ("_closed", "bool"),
            )
        }
        _require_fields(context, composed, "CTX002", violations)
        _check_close_once(context, violations)

    location = by_path.get(
        "src/sidekick_usages/persistence/migrations/location.py"
    )
    expected_codes = {
        "EMPTY": "empty",
        "PROTOTYPE_ONLY": "prototype_only",
        "COMPATIBILITY_SELECTED": "compatibility_selected",
        "CANONICAL_SELECTED": "canonical_selected",
        "EQUIVALENT_SELECTED": "equivalent_selected",
        "CONFLICT": "conflict",
        "PARTIAL": "partial",
        "CANDIDATE_BLOCKED": "candidate_blocked",
    }
    if (
        location is not None
        and enum_values(location.tree, "LocationCode") != expected_codes
    ):
        violations.append(
            finding(location, None, "MODEL001", "LocationCode changed")
        )


def _require_fields(
    unit: SourceUnit,
    expected: Mapping[str, tuple[tuple[str, str], ...]],
    rule_id: str,
    violations: list[ArchitectureFinding],
) -> None:
    for name, fields in expected.items():
        if class_fields(unit.tree, name) != fields:
            violations.append(
                finding(
                    unit,
                    class_node(unit.tree, name),
                    rule_id,
                    f"{name} differs from its frozen field contract",
                )
            )


def _check_close_once(
    context: SourceUnit,
    violations: list[ArchitectureFinding],
) -> None:
    registrations = [
        node
        for node in ast.walk(context.tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "call_on_close"
    ]
    registered = [
        compact(ast.unparse(argument))
        for call in registrations
        for argument in call.args
    ]
    has_setter = any(
        isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "set_context"
        for node in ast.walk(context.tree)
    )
    implicit_registry_default = any(
        isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.Or)
        and {
            child.id for child in ast.walk(node) if isinstance(child, ast.Name)
        }
        & {"providers", "heartbeat_providers"}
        for node in ast.walk(context.tree)
    )
    context_singleton = any(
        isinstance(statement, (ast.AnnAssign, ast.Assign))
        and isinstance(statement.value, ast.Call)
        and dotted_name(statement.value.func)
        in {"AppContext", "Composed", "InvocationContext"}
        for statement in context.tree.body
    )
    if (
        len(registrations) != 1
        or registered != ["owner.close"]
        or has_setter
        or implicit_registry_default
        or context_singleton
    ):
        violations.append(
            finding(
                context,
                registrations[0] if registrations else None,
                "CTX002",
                "one lazy Composed owner must register close exactly once",
            )
        )


__all__ = ["check_value_contracts"]

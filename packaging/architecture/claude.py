"""Managed Claude authentication architecture contracts."""

import ast
from collections.abc import Sequence

from architecture.models import ArchitectureFinding, SourceUnit
from architecture.source import dotted_name, finding

_CLAUDE_CONFIG_OWNER_PATHS = frozenset(
    {
        "src/sidekick_usages/providers/claude/activity.py",
        "src/sidekick_usages/providers/claude/environment.py",
    }
)
_CLAUDE_CONFIG_VARIABLE = "CLAUDE_CONFIG_DIR"
_CREDENTIAL_FILE = ".credentials.json"
_CREDENTIAL_FILE_OWNERS = frozenset(
    {
        "src/sidekick_usages/credentials/claude/managed/authority/service.py",
        "src/sidekick_usages/persistence/credentials/refresh/service.py",
        "src/sidekick_usages/providers/claude/credentials.py",
    }
)
_EXECUTABLE_OWNER = "src/sidekick_usages/platform/executable.py"
_KEYCHAIN_MUTATIONS = frozenset(
    {
        "add-generic-password",
        "delete-generic-password",
    }
)
_KEYCHAIN_OWNER = (
    "src/sidekick_usages/providers/claude/managed/storage/keychain.py"
)
_KEYCHAIN_READ_CONSTANTS = frozenset(
    {
        "/usr/bin/security",
        "Claude Code-credentials",
        "Claude Code-credentials-",
        "find-generic-password",
    }
)
_PROHIBITED_FLAT_MODULES = frozenset(
    {
        "src/sidekick_usages/credentials/claude/managed/authority.py",
        "src/sidekick_usages/credentials/claude_activation.py",
        "src/sidekick_usages/credentials/claude_authorities.py",
        "src/sidekick_usages/credentials/claude_migration.py",
        "src/sidekick_usages/credentials/claude_reconciliation.py",
        "src/sidekick_usages/persistence/claude_authorities.py",
        "src/sidekick_usages/providers/claude/auth_status.py",
        "src/sidekick_usages/providers/claude/authority.py",
        "src/sidekick_usages/providers/claude/capabilities.py",
        "src/sidekick_usages/providers/claude/executable.py",
        "src/sidekick_usages/providers/claude/keychain.py",
        "src/sidekick_usages/providers/claude/login.py",
        "src/sidekick_usages/providers/claude/profiles.py",
    }
)


def check_claude_auth_ownership(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Reject managed Claude boundaries outside their exact owners."""
    for unit in units:
        if not unit.production:
            continue
        node = _claude_violation(unit)
        if node is not None:
            violations.append(
                finding(
                    unit,
                    node,
                    "CLAUDE001",
                    "Managed Claude auth must remain in qualified owners",
                )
            )


def _claude_violation(unit: SourceUnit) -> ast.AST | None:
    path = unit.path.as_posix()
    if path in _PROHIBITED_FLAT_MODULES:
        return unit.tree
    for node in ast.walk(unit.tree):
        if _forbidden_storage_constant(path, node):
            return node
        if (
            path not in _CLAUDE_CONFIG_OWNER_PATHS
            and isinstance(node, ast.Constant)
            and node.value == _CLAUDE_CONFIG_VARIABLE
        ):
            return node
        if (
            path != _EXECUTABLE_OWNER
            and isinstance(node, ast.Call)
            and dotted_name(node.func) == "shutil.which"
            and any(
                isinstance(argument, ast.Constant)
                and argument.value == "claude"
                for argument in node.args
            )
        ):
            return node
    return None


def _forbidden_storage_constant(path: str, node: ast.AST) -> bool:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        return False
    return (
        node.value in _KEYCHAIN_MUTATIONS
        or (path != _KEYCHAIN_OWNER and node.value in _KEYCHAIN_READ_CONSTANTS)
        or (
            path not in _CREDENTIAL_FILE_OWNERS
            and node.value == _CREDENTIAL_FILE
        )
    )

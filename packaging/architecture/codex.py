"""Codex authentication architecture contracts."""

import ast
from collections.abc import Sequence

from architecture.models import ArchitectureFinding, SourceUnit
from architecture.source import finding

_RETIRED_AUTH_MODULES = frozenset(
    {
        "src/sidekick_usages/credentials/codex/coordinator.py",
    }
)
_RETIRED_AUTH_NAMES = frozenset(
    {
        "CODEX_REFRESH",
        "CodexCredentialCoordinator",
        "PreparedCodexAuthBundle",
        "codex_export_cmd",
        "export_codex",
        "prepare_export_bundle",
        "prepare_private_bundle",
        "validate_auth_bundle_owner",
        "validate_auth_bundle_matches_account",
    }
)
_RETIRED_PROVIDER_AUTH_NAMES = frozenset(
    {
        "RefreshPayload",
        "validate_refresh_payload",
    }
)
_RETIRED_AUTH_VALUES = frozenset(
    {
        "https://auth.openai.com/oauth/token",
        "--codex-home",
    }
)


def check_codex_auth_ownership(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Reject retired Codex token exchange, copy, and export mechanisms."""
    for unit in units:
        if not unit.production:
            continue
        node = _retired_auth_node(unit)
        if node is not None:
            violations.append(
                finding(
                    unit,
                    node,
                    "CODEX001",
                    "Codex credentials belong only to official managed homes",
                )
            )


def _retired_auth_node(unit: SourceUnit) -> ast.AST | None:
    """Return the first structural use of one retired Codex auth surface."""
    path = str(unit.path)
    if path in _RETIRED_AUTH_MODULES:
        return unit.tree
    codex_owner = "/providers/codex/" in path or "/credentials/codex/" in path
    for node in ast.walk(unit.tree):
        name = _declaration_or_reference_name(node)
        if name in _RETIRED_AUTH_NAMES:
            return node
        if codex_owner and name in _RETIRED_PROVIDER_AUTH_NAMES:
            return node
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in _RETIRED_AUTH_VALUES
        ):
            return node
    return None


def _declaration_or_reference_name(node: ast.AST) -> str | None:
    """Return one declared or referenced source identifier."""
    if isinstance(node, ast.ClassDef | ast.FunctionDef):
        return node.name
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None

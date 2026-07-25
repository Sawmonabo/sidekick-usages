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
_NATIVE_HOME_OWNER = "src/sidekick_usages/providers/codex/native.py"
_NATIVE_HOME_CONSUMERS = frozenset(
    {
        _NATIVE_HOME_OWNER,
        "src/sidekick_usages/entrypoints/supervisor.py",
        "src/sidekick_usages/entrypoints/worker.py",
        "src/sidekick_usages/providers/codex/auth.py",
    }
)
_NATIVE_HOME_LITERALS = frozenset({".codex", "CODEX_HOME"})
_DEFAULT_CODEX_HOME_NAME = "default_codex_home"
_AUTH_OWNER = "src/sidekick_usages/providers/codex/auth.py"
_MANAGED_AUTH_READER = "src/sidekick_usages/credentials/codex/managed/home.py"
_AUTH_REFERENCE_OWNERS = frozenset({_AUTH_OWNER, _MANAGED_AUTH_READER})
_AUTH_BASENAME = "auth.json"
_AUTH_NAME = "CODEX_AUTH_FILE"
_MANAGED_AUTH_READ_CALL = "read_relative_bundle_file"
_AUTH_MUTATION_CALLS = frozenset(
    {
        "copy",
        "copy2",
        "copyfile",
        "copytree",
        "move",
        "rename",
        "replace",
        "write",
        "write_bytes",
        "write_text",
    }
)
_TOKEN_SERIALIZATION_CALLS = frozenset(
    {
        "dump",
        "dumps",
        "encode_compact_json_buffer",
        "encode_json",
        "encode_json_rpc_message",
        "encode_worker_message",
        "model_dump_json",
    }
)
_CODEX_TOKEN_FIELDS = frozenset(
    {
        "accessToken",
        "access_token",
        "authToken",
        "idToken",
        "id_token",
        "refresh_token",
    }
)
_QUALIFIED_CODEX_TOKEN_CODECS = frozenset(
    {
        "src/sidekick_usages/persistence/schema/credential.py",
        "src/sidekick_usages/providers/codex/broker/"
        "external_auth/activation.py",
        "src/sidekick_usages/providers/codex/broker/external_auth/refresh.py",
    }
)


def check_codex_auth_ownership(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Reject Codex authentication outside its exact owners."""
    for unit in units:
        if not unit.production:
            continue
        node = _codex_auth_violation(unit)
        if node is not None:
            violations.append(
                finding(
                    unit,
                    node,
                    "CODEX001",
                    "Codex authentication must remain in qualified owners",
                )
            )


def _codex_auth_violation(unit: SourceUnit) -> ast.AST | None:
    """Return the first Codex authentication ownership violation."""
    path = unit.path.as_posix()
    if path in _RETIRED_AUTH_MODULES:
        return unit.tree
    checks = (
        _retired_auth_node(unit, path),
        _native_home_violation(unit, path),
        _auth_reference_violation(unit, path),
        _token_serialization_violation(unit, path),
    )
    return next((node for node in checks if node is not None), None)


def _retired_auth_node(
    unit: SourceUnit,
    path: str,
) -> ast.AST | None:
    """Return one retired OAuth, copy, or export surface."""
    codex_owner = "/providers/codex/" in path or "/credentials/codex/" in path
    for node in ast.walk(unit.tree):
        name = _source_name(node)
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


def _native_home_violation(
    unit: SourceUnit,
    path: str,
) -> ast.AST | None:
    """Return native-home discovery outside its canonical boundary."""
    if path != _NATIVE_HOME_OWNER:
        literal = _first_string(unit.tree, _NATIVE_HOME_LITERALS)
        if literal is not None:
            return literal
    if path in _NATIVE_HOME_CONSUMERS:
        return None
    return _first_named_node(unit.tree, _DEFAULT_CODEX_HOME_NAME)


def _auth_reference_violation(
    unit: SourceUnit,
    path: str,
) -> ast.AST | None:
    """Return direct auth-file access outside the two qualified readers."""
    if path not in _AUTH_REFERENCE_OWNERS:
        return next(
            (node for node in ast.walk(unit.tree) if _auth_reference(node)),
            None,
        )
    if path == _AUTH_OWNER:
        return next(
            (
                node
                for node in ast.walk(unit.tree)
                if isinstance(node, ast.Call) and _mutates_auth(node)
            ),
            None,
        )
    allowed_nodes = {
        id(child)
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.Call)
        and _call_name(node) == _MANAGED_AUTH_READ_CALL
        for child in ast.walk(node)
    }
    return next(
        (
            node
            for node in ast.walk(unit.tree)
            if _auth_reference(node) and id(node) not in allowed_nodes
        ),
        None,
    )


def _token_serialization_violation(
    unit: SourceUnit,
    path: str,
) -> ast.AST | None:
    """Return a Codex token codec outside its qualified protocol owners."""
    if path in _QUALIFIED_CODEX_TOKEN_CODECS:
        return None
    return next(
        (
            node
            for node in ast.walk(unit.tree)
            if isinstance(node, ast.Call)
            and _call_name(node) in _TOKEN_SERIALIZATION_CALLS
            and _first_string(node, _CODEX_TOKEN_FIELDS) is not None
        ),
        None,
    )


def _auth_reference(node: ast.AST) -> bool:
    """Return whether one node names the canonical Codex auth file."""
    return (
        (isinstance(node, ast.Constant) and node.value == _AUTH_BASENAME)
        or (isinstance(node, ast.Name) and node.id == _AUTH_NAME)
        or (isinstance(node, ast.Attribute) and node.attr == _AUTH_NAME)
    )


def _mutates_auth(node: ast.Call) -> bool:
    """Return whether one call can mutate the auth owner's filesystem."""
    name = _call_name(node)
    if name in _AUTH_MUTATION_CALLS:
        return True
    if name != "open":
        return False
    mode = _open_mode(node)
    return mode is None or bool(set(mode).intersection("awx+"))


def _open_mode(node: ast.Call) -> str | None:
    """Return one statically declared file-open mode."""
    for keyword in node.keywords:
        if keyword.arg == "mode":
            return _string_value(keyword.value)
    index = 0 if isinstance(node.func, ast.Attribute) else 1
    if len(node.args) <= index:
        return "r"
    return _string_value(node.args[index])


def _string_value(node: ast.AST) -> str | None:
    """Return one literal string value."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _first_string(
    tree: ast.AST,
    values: frozenset[str],
) -> ast.Constant | None:
    """Return the first exact string in ``values``."""
    return next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in values
        ),
        None,
    )


def _first_named_node(tree: ast.AST, name: str) -> ast.AST | None:
    """Return the first declaration, import, or reference to ``name``."""
    return next(
        (node for node in ast.walk(tree) if _source_name(node) == name),
        None,
    )


def _call_name(node: ast.Call) -> str | None:
    """Return one direct or attributed call name."""
    function = node.func
    if isinstance(function, ast.Name):
        return function.id
    if isinstance(function, ast.Attribute):
        return function.attr
    return None


def _source_name(node: ast.AST) -> str | None:
    """Return one declared, imported, or referenced identifier."""
    if isinstance(node, ast.ClassDef | ast.FunctionDef):
        return node.name
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.alias):
        return node.name.rsplit(".", 1)[-1]
    return None

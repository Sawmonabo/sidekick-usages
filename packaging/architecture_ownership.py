"""Single-owner architecture rules for paths, migrations, HTTP, and brand."""

import ast
from collections.abc import Sequence
from pathlib import PurePosixPath

from architecture_ast import (
    ROBOT_ART,
    ArchitectureFinding,
    SourceUnit,
    assigned_names,
    assignment_literal,
    contains_call,
    contains_string,
    dotted_name,
    finding,
    function_node,
    matches,
    scan_imports,
)


def check_ownership(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Enforce each approved single-owner production contract."""
    production = tuple(unit for unit in units if unit.production)
    _check_paths(production, violations)
    _check_migrations(production, violations)
    _check_http(production, violations)
    _check_brand(production, violations)


def _check_paths(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    owners = {
        str(unit.path)
        for unit in units
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.ClassDef) and node.name == "ApplicationPaths"
    }
    for unit in units:
        path = str(unit.path)
        for node in ast.walk(unit.tree):
            if (
                isinstance(node, ast.BinOp)
                and contains_call(node, "Path.home")
                and contains_string(node, "sidekick-usages")
                and path != "src/sidekick_usages/paths.py"
            ):
                violations.append(
                    finding(unit, node, "PATH002", "duplicate Sidekick root")
                )
            if (
                isinstance(node, ast.Call)
                and dotted_name(node.func) in {"Path", "PurePath"}
                and any(
                    isinstance(child, ast.Constant)
                    and isinstance(child.value, str)
                    and "sidekick-usages" in child.value
                    for child in ast.walk(node)
                )
                and path != "src/sidekick_usages/paths.py"
            ):
                violations.append(
                    finding(unit, node, "PATH002", "duplicate Sidekick path")
                )
        if path == "src/sidekick_usages/paths.py":
            continue
        for statement in unit.tree.body:
            if isinstance(
                statement,
                (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef),
            ):
                continue
            if any(
                isinstance(node, ast.Call)
                and dotted_name(node.func) == "discover_application_paths"
                for node in ast.walk(statement)
            ):
                violations.append(
                    finding(
                        unit,
                        statement,
                        "PATH002",
                        "import-time path discovery",
                    )
                )
    if owners != {"src/sidekick_usages/paths.py"}:
        violations.append(
            ArchitectureFinding(
                PurePosixPath("src/sidekick_usages/paths.py"),
                1,
                "PATH002",
                f"ApplicationPaths owners are {sorted(owners)}",
            )
        )


def _check_migrations(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    owner = "src/sidekick_usages/persistence/migrations/service.py"
    rollback_owner = (
        "src/sidekick_usages/persistence/migrations/rollback.py"
    )
    for unit in units:
        if str(unit.path) in {owner, rollback_owner}:
            continue
        for node in ast.walk(unit.tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "commit_migration"
            ):
                violations.append(
                    finding(
                        unit,
                        node,
                        "MIG001",
                        "second migration coordinator",
                    )
                )
    service = next((unit for unit in units if str(unit.path) == owner), None)
    has_port = service is not None and any(
        matches(module, "sidekick_usages.persistence.migrations.ports")
        and isinstance(node, ast.ImportFrom)
        and any(alias.name == "PrivateAuthMigrator" for alias in node.names)
        for node, module in scan_imports(service)
    )
    if service is not None and not has_port:
        violations.append(
            finding(
                service,
                None,
                "MIG001",
                "PrivateAuthMigrator port missing",
            )
        )


def _check_http(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    policy_owners: set[str] = set()
    executor_callers: set[str] = set()
    for unit in units:
        path = str(unit.path)
        for node in ast.walk(unit.tree):
            if isinstance(node, (ast.AnnAssign, ast.Assign)) and any(
                target in {"_POLICIES", "_RETRY_POLICIES"}
                for target in assigned_names(node)
            ):
                policy_owners.add(path)
            if (
                isinstance(node, ast.Call)
                and dotted_name(node.func) == "RetryExecutor"
            ):
                executor_callers.add(path)
    valid = policy_owners == {"src/sidekick_usages/http/retry.py"} and (
        executor_callers == {"src/sidekick_usages/http/client.py"}
    )
    client = next(
        (
            unit
            for unit in units
            if str(unit.path) == "src/sidekick_usages/http/client.py"
        ),
        None,
    )
    if client is not None:
        valid = valid and _client_contract_is_closed(client)
    if not valid:
        violations.append(
            ArchitectureFinding(
                PurePosixPath("src/sidekick_usages/http/retry.py"),
                1,
                "HTTP001",
                "retry owner, HTTPMethod, or facade type contract changed",
            )
        )


def _client_contract_is_closed(client: SourceUnit) -> bool:
    request = function_node(client.tree, "_request")
    annotation = (
        request.args.args[1].annotation
        if request is not None and len(request.args.args) > 1
        else None
    )
    typed = annotation is not None and ast.unparse(annotation) == "HTTPMethod"
    constructed = not any(
        isinstance(node, ast.Call)
        and dotted_name(node.func) == "self._request"
        and (
            not node.args
            or dotted_name(node.args[0])
            not in {"HTTPMethod.GET", "HTTPMethod.POST"}
        )
        for node in ast.walk(client.tree)
    )
    facade_closed = not any(
        isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and not node.name.startswith("_")
        and any(
            annotation is not None and "urllib3" in ast.unparse(annotation)
            for annotation in (
                node.returns,
                *(argument.annotation for argument in node.args.args),
                *(argument.annotation for argument in node.args.kwonlyargs),
            )
        )
        for node in ast.walk(client.tree)
    )
    return typed and constructed and facade_closed


def _check_brand(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    owners = {
        str(unit.path)
        for unit in units
        for node in unit.tree.body
        if isinstance(node, (ast.AnnAssign, ast.Assign))
        and "ROBOT_LINES" in assigned_names(node)
    }
    branding = next(
        (
            unit
            for unit in units
            if str(unit.path) == "src/sidekick_usages/branding.py"
        ),
        None,
    )
    valid = owners == {"src/sidekick_usages/branding.py"} and (
        branding is not None
        and assignment_literal(branding.tree, "ROBOT_LINES") == ROBOT_ART
    )
    if not valid:
        violations.append(
            ArchitectureFinding(
                PurePosixPath("src/sidekick_usages/branding.py"),
                1,
                "BRAND001",
                "robot art must have one exact canonical source",
            )
        )

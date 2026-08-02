"""Repository package and namespace shape checks."""

import ast
from collections.abc import Sequence
from pathlib import PurePosixPath

from architecture.models import ArchitectureFinding, SourceUnit
from architecture.source import finding

MIN_NAMESPACE_FAMILY_SIZE = 2
# The approved selection contract requires this one schema sibling.
_APPROVED_FLAT_NAMESPACE_FAMILIES = frozenset(
    {
        (
            PurePosixPath("src/sidekick_usages/persistence/schema"),
            "prefix",
            "selection",
        ),
    }
)
_STALE_SOURCE_FILES = frozenset(
    {
        "src/sidekick_usages/cli.py",
        "src/sidekick_usages/cli_help.py",
        "src/sidekick_usages/cli/runtime/dashboard.py",
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
        "src/sidekick_usages/daemon/lifecycle/launchd.py",
        "src/sidekick_usages/daemon/lifecycle/platform.py",
        "src/sidekick_usages/daemon/lifecycle/systemd.py",
        "src/sidekick_usages/daemon/lifecycle/wsl.py",
        "src/sidekick_usages/daemon/peer.py",
        "src/sidekick_usages/daemon/protocol.py",
        "src/sidekick_usages/daemon/recovery.py",
        "src/sidekick_usages/daemon/runtime/callbacks.py",
        "src/sidekick_usages/daemon/runtime/entrypoint.py",
        "src/sidekick_usages/daemon/scheduler.py",
        "src/sidekick_usages/daemon/supervisor.py",
        "src/sidekick_usages/daemon/worker_entrypoint.py",
        "src/sidekick_usages/daemon/worker/codex.py",
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
        "src/sidekick_usages/persistence/_recovery.py",
        "src/sidekick_usages/persistence/account_index.py",
        "src/sidekick_usages/persistence/account_runtime_bridge.py",
        "src/sidekick_usages/persistence/account_store.py",
        "src/sidekick_usages/persistence/activity_snapshots.py",
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
        "src/sidekick_usages/persistence/usage_snapshots.py",
        "src/sidekick_usages/providers/claude.py",
        "src/sidekick_usages/providers/claude/credential_schemas.py",
        "src/sidekick_usages/providers/claude/schemas.py",
        "src/sidekick_usages/providers/codex.py",
        "src/sidekick_usages/providers/codex/account.py",
        "src/sidekick_usages/providers/codex/app_server.py",
        "src/sidekick_usages/providers/codex/auth.py",
        "src/sidekick_usages/providers/codex/capabilities.py",
        "src/sidekick_usages/providers/codex/errors.py",
        "src/sidekick_usages/providers/codex/executable.py",
        "src/sidekick_usages/providers/codex/generation.py",
        "src/sidekick_usages/providers/codex/jsonrpc.py",
        "src/sidekick_usages/providers/codex/login.py",
        "src/sidekick_usages/providers/codex/models.py",
        "src/sidekick_usages/providers/codex/models/app_server.py",
        "src/sidekick_usages/providers/codex/native.py",
        "src/sidekick_usages/providers/codex/process.py",
        "src/sidekick_usages/providers/codex/request.py",
        "src/sidekick_usages/providers/codex/schemas.py",
        "src/sidekick_usages/providers/codex/token.py",
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
_OWNER_MODULE_TOKENS = frozenset(
    {
        "error",
        "errors",
        "model",
        "models",
        "port",
        "ports",
        "schema",
        "schemas",
        "type",
        "types",
    }
)
_ROOT_INITIALIZER = PurePosixPath("src/sidekick_usages/__init__.py")


def check_source_shape(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Enforce clean package initializers and cohesive file namespaces."""
    present = {str(unit.path) for unit in units}
    stale = sorted(_STALE_SOURCE_FILES & present)
    if stale:
        violations.append(
            ArchitectureFinding(
                PurePosixPath("src/sidekick_usages"),
                1,
                "PKG001",
                f"stale converted modules remain: {stale}",
            )
        )
    _check_private_package_names(units, violations)
    _check_flat_namespaces(units, violations)
    _check_owner_module_names(units, violations)
    for unit in units:
        if not _repository_code(unit) or unit.path.name != "__init__.py":
            continue
        invalid = next(
            (
                node
                for index, node in enumerate(unit.tree.body)
                if not _initializer_statement_allowed(unit.path, index, node)
            ),
            None,
        )
        if invalid is not None:
            violations.append(
                finding(unit, invalid, "PKG001", "initializer is not thin")
            )


def _check_private_package_names(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    for unit in units:
        if not _repository_code(unit):
            continue
        private = next(
            (part for part in unit.path.parent.parts if part.startswith("_")),
            None,
        )
        if private is not None:
            violations.append(
                finding(
                    unit,
                    None,
                    "PKG001",
                    f"package directory {private!r} cannot be private",
                )
            )


def _initializer_statement_allowed(
    path: PurePosixPath,
    index: int,
    node: ast.stmt,
) -> bool:
    if (
        index == 0
        and isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    ):
        return True
    return (
        path == _ROOT_INITIALIZER
        and isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "__version__"
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _check_owner_module_names(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    for unit in units:
        if not _repository_code(unit) or unit.path.name == "__init__.py":
            continue
        tokens = frozenset(unit.path.stem.split("_"))
        invalid = unit.path.stem == "contracts" or (
            len(tokens) > 1 and bool(tokens & _OWNER_MODULE_TOKENS)
        )
        if invalid:
            violations.append(
                finding(
                    unit,
                    None,
                    "PKG003",
                    "owner types, models, schemas, ports, and errors "
                    "require designated modules",
                )
            )


def _check_flat_namespaces(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    families: dict[
        tuple[PurePosixPath, str, str],
        list[SourceUnit],
    ] = {}
    for unit in units:
        if not _repository_code(unit) or unit.path.name == "__init__.py":
            continue
        stem = unit.path.stem
        if stem.startswith("__"):
            continue
        tokens = stem.split("_")
        families.setdefault(
            (unit.path.parent, "prefix", tokens[0]),
            [],
        ).append(unit)
        if len(tokens) > 1:
            families.setdefault(
                (unit.path.parent, "suffix", tokens[-1]),
                [],
            ).append(unit)
    for (parent, kind, token), members in sorted(
        families.items(),
        key=lambda item: tuple(str(value) for value in item[0]),
    ):
        if (parent, kind, token) in _APPROVED_FLAT_NAMESPACE_FAMILIES:
            continue
        if len(members) < MIN_NAMESPACE_FAMILY_SIZE:
            continue
        names = sorted(unit.path.name for unit in members)
        violations.append(
            ArchitectureFinding(
                min(unit.path for unit in members),
                1,
                "PKG002",
                f"flat {kind} family {token!r} in {parent}: {names}",
            )
        )


def _repository_code(unit: SourceUnit) -> bool:
    return unit.production or unit.packaging

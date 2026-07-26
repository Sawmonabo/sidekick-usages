"""Import-boundary and rendering architecture contracts."""

import ast
from collections.abc import Sequence

from architecture.models import ArchitectureFinding, SourceUnit
from architecture.source import finding, matches, matches_any, scan_imports

_CODEX_JSONRPC_ROOT = "src/sidekick_usages/providers/codex/app_server/jsonrpc/"
_CODEX_BROKER_WIRE_FILE = (
    "src/sidekick_usages/providers/codex/broker/wire.py"
)
_CACHED_DASHBOARD_SERVICE_FILE = (
    "src/sidekick_usages/usage/dashboard/service.py"
)
_DASHBOARD_ENTRYPOINT_FILE = "src/sidekick_usages/entrypoints/dashboard.py"
_DASHBOARD_APPLICATION_FILE = (
    "src/sidekick_usages/cli/dashboard/application.py"
)
_DASHBOARD_INPUT_FILE = "src/sidekick_usages/cli/dashboard/input.py"
_PROMPT_TOOLKIT_FILES = frozenset(
    {
        _DASHBOARD_APPLICATION_FILE,
        _DASHBOARD_INPUT_FILE,
    }
)
_ISOLATED_WORKER_FILES = frozenset(
    {
        "src/sidekick_usages/daemon/worker/account.py",
        "src/sidekick_usages/daemon/worker/claude/maintenance.py",
        "src/sidekick_usages/daemon/worker/claude/selection.py",
        "src/sidekick_usages/daemon/worker/codex.py",
        "src/sidekick_usages/daemon/worker/ports.py",
    }
)
_SUPERVISOR_ENTRYPOINT_FILE = "src/sidekick_usages/entrypoints/supervisor.py"
_SUPERVISOR_PROVIDER_IMPORTS = frozenset(
    {
        "sidekick_usages.providers.codex.app_server.executable",
        "sidekick_usages.providers.codex.broker.responder",
        "sidekick_usages.providers.codex.broker.service",
        "sidekick_usages.providers.codex.native",
    }
)
_SERVICE_FILES = frozenset(
    {
        "src/sidekick_usages/credentials/service.py",
        "src/sidekick_usages/heartbeat/service.py",
        "src/sidekick_usages/maintenance.py",
        "src/sidekick_usages/update.py",
        "src/sidekick_usages/usage/activity.py",
        "src/sidekick_usages/usage/lookup/service.py",
        "src/sidekick_usages/usage/service.py",
    }
)
_RENDERER_FILES = frozenset(
    {
        "src/sidekick_usages/branding.py",
        "src/sidekick_usages/heartbeat/render.py",
        "src/sidekick_usages/usage/presentation/activity.py",
        "src/sidekick_usages/usage/presentation/dashboard/footer.py",
        "src/sidekick_usages/usage/presentation/dashboard/overview.py",
        "src/sidekick_usages/usage/presentation/dashboard/selection.py",
        "src/sidekick_usages/usage/presentation/narrow.py",
        "src/sidekick_usages/usage/presentation/overview.py",
        "src/sidekick_usages/usage/presentation/reset.py",
    }
)
_CREDENTIAL_LEASE_CONSUMERS = frozenset(
    {
        "src/sidekick_usages/cli/context.py",
        "src/sidekick_usages/credentials/authorities.py",
        (
            "src/sidekick_usages/credentials/claude/managed/"
            "migration/service.py"
        ),
        "src/sidekick_usages/credentials/codex/managed/resolver.py",
        "src/sidekick_usages/credentials/refresh.py",
        "src/sidekick_usages/daemon/worker/account.py",
        "src/sidekick_usages/heartbeat/service.py",
        "src/sidekick_usages/usage/activity.py",
        "src/sidekick_usages/usage/lookup/service.py",
        "src/sidekick_usages/usage/service.py",
    }
)
_DAEMON_CONTROL_FILES = frozenset(
    {
        "src/sidekick_usages/daemon/control/protocol.py",
        "src/sidekick_usages/platform/peer.py",
    }
)
_PYDANTIC_OWNERS = frozenset(
    {
        "src/sidekick_usages/providers/claude/schema/credentials.py",
        "src/sidekick_usages/providers/claude/schema/usage.py",
        "src/sidekick_usages/providers/codex/schemas.py",
        "src/sidekick_usages/serialization/json.py",
    }
)
_TRANSPORT_ROOTS = frozenset({"httpx", "requests", "tenacity", "urllib3"})


def check_import_boundaries(
    units: Sequence[SourceUnit],
    violations: list[ArchitectureFinding],
) -> None:
    """Enforce directed package dependencies and narrow import owners."""
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
            "/cli/" not in path
            and not path.endswith("/__main__.py")
            and path != _DASHBOARD_ENTRYPOINT_FILE,
            matches(module, "sidekick_usages.cli"),
            "non-CLI code cannot import CLI composition",
        ),
        (
            "DEP006",
            root == "prompt_toolkit",
            path not in _PROMPT_TOOLKIT_FILES,
            "prompt-toolkit belongs only to the isolated dashboard process",
        ),
        (
            "DEP006",
            path.startswith("src/")
            and path
            not in {
                _DASHBOARD_ENTRYPOINT_FILE,
                _DASHBOARD_APPLICATION_FILE,
            },
            matches(
                module,
                "sidekick_usages.cli.dashboard.application",
            ),
            "only the dashboard entrypoint can reach interactive imports",
        ),
        (
            "DEP006",
            path.startswith("src/") and path != _DASHBOARD_APPLICATION_FILE,
            matches(module, "sidekick_usages.cli.dashboard.input"),
            "dashboard input is private to its isolated application",
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
            "DEP004",
            path != _CODEX_BROKER_WIRE_FILE,
            root == "websockets",
            "WebSocket transport belongs only to the Codex broker wire",
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
            "DEP006",
            _is_resident_daemon(path),
            matches_any(
                module,
                (
                    "click",
                    "prompt_toolkit",
                    "rich",
                    "typer",
                    "sidekick_usages.cli",
                    "sidekick_usages.credentials",
                    "sidekick_usages.http",
                    "sidekick_usages.providers",
                    "sidekick_usages.usage",
                ),
            ),
            "resident daemon code cannot import heavy application owners",
        ),
        (
            "DEP006",
            not path.startswith("src/sidekick_usages/entrypoints/"),
            matches(module, "sidekick_usages.entrypoints"),
            "composition entrypoints cannot be imported as application code",
        ),
        (
            "DEP006",
            path == _SUPERVISOR_ENTRYPOINT_FILE,
            (
                matches_any(
                    module,
                    (
                        "click",
                        "prompt_toolkit",
                        "rich",
                        "typer",
                        "sidekick_usages.cli",
                        "sidekick_usages.credentials",
                        "sidekick_usages.http",
                        "sidekick_usages.usage",
                    ),
                )
                or (
                    matches(module, "sidekick_usages.providers")
                    and not any(
                        matches(module, allowed)
                        for allowed in _SUPERVISOR_PROVIDER_IMPORTS
                    )
                )
            ),
            "supervisor entrypoint imports only its lean Codex boundary",
        ),
        (
            "DEP006",
            path in _DAEMON_CONTROL_FILES,
            matches(module, "sidekick_usages.persistence"),
            "daemon control primitives cannot import persistence",
        ),
        (
            "DEP006",
            path == _CACHED_DASHBOARD_SERVICE_FILE,
            matches_any(
                module,
                (
                    "sidekick_usages.credentials",
                    "sidekick_usages.daemon.lifecycle",
                    "sidekick_usages.daemon.worker",
                    "sidekick_usages.http",
                    "sidekick_usages.maintenance",
                    "sidekick_usages.persistence.accounts.store",
                    "sidekick_usages.persistence.credentials",
                    "sidekick_usages.persistence.private",
                    "sidekick_usages.persistence.service",
                    "sidekick_usages.providers",
                ),
            ),
            "cached dashboard cannot compose credential or provider graphs",
        ),
        (
            "PATH001",
            not path.endswith("/paths.py"),
            matches(module, "platformdirs"),
            "platformdirs is private to paths.py",
        ),
        (
            "SCHEMA001",
            not _is_pydantic_owner(path),
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
                "sidekick_usages.daemon",
                "sidekick_usages.heartbeat.service",
                "sidekick_usages.usage",
            ),
        )
        persistence_leak = matches(module, "sidekick_usages.persistence")
        jsonrpc_leak = path.startswith(_CODEX_JSONRPC_ROOT) and matches(
            module,
            "sidekick_usages.http",
        )
        if forbidden_provider or persistence_leak or jsonrpc_leak:
            violations.append(
                finding(
                    unit,
                    node,
                    "DEP004",
                    "provider adapter crosses an unapproved boundary",
                )
            )


def _is_resident_daemon(path: str) -> bool:
    prefix = "src/sidekick_usages/daemon/"
    return path.startswith(prefix) and path not in _ISOLATED_WORKER_FILES


def _is_pydantic_owner(path: str) -> bool:
    return path in _PYDANTIC_OWNERS or path.startswith(
        "src/sidekick_usages/persistence/schema/"
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

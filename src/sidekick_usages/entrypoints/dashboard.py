"""Dedicated interactive dashboard process image."""

import os
import sys
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import TextIO

from sidekick_usages.cli.contexts.composition import (
    ApplicationCompositionError,
    compose_claude_migration,
)
from sidekick_usages.cli.contexts.dashboard.snapshot import (
    CachedDashboardSnapshotSource,
)
from sidekick_usages.cli.dashboard.application import (
    InteractiveDashboardApplication,
)
from sidekick_usages.cli.dashboard.models.controller import (
    ClaudeAssociationRequest,
    DashboardApplicationResult,
)
from sidekick_usages.cli.dashboard.session import (
    InteractiveDashboardSession,
)
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.cli.runtime.routing import parse_dashboard_arguments
from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.core.accounts.models import ClaudeAccountAuthority
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.credentials.capabilities.service import (
    build_provider_capability_service,
)
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.lifecycle.manager import build_daemon_manager
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.persistence.accounts.reader import AccountIndexReader
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.setup.store import (
    ServiceSetupAcknowledgementStore,
)
from sidekick_usages.providers.base import ProviderFailure
from sidekick_usages.providers.claude.managed.executable import (
    resolve_claude_launcher,
)
from sidekick_usages.providers.codex.app_server.executable import (
    resolve_codex_launcher,
)
from sidekick_usages.usage.lookup.worker.client import (
    UsageLookupModuleLaunchPlanner,
    UsageLookupWorkerClient,
    resolve_usage_lookup_interpreter,
)
from sidekick_usages.usage.presentation.formatting import (
    sanitize_terminal_text,
)

INVALID_INVOCATION_EXIT_CODE = 2
CLAUDE_ASSOCIATION_PROMPT = (
    "Connect '{label}' for future Claude switching?\n"
    "This keeps its setup token and does not change the active Claude "
    "account. [y/N]"
)
CLAUDE_ASSOCIATION_APPROVALS = frozenset({"y", "yes"})
CLAUDE_ASSOCIATION_READ_FAILURE = (
    "Saved Claude account metadata could not be read."
)


def _connect_dashboard_control(socket_path: Path) -> ControlClient:
    """Open one bounded local supervisor connection."""
    return ControlClient.connect(socket_path)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the private interactive entry point on supported Unix platforms."""
    if sys.platform == "win32":
        return int(ExitCode.MANUAL_ACTION)
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        only = parse_dashboard_arguments(arguments)
    except ValueError:
        return INVALID_INVOCATION_EXIT_CODE
    paths = discover_application_paths()
    clock = SystemClock()
    return _run_dashboard_loop(
        partial(_run_dashboard_once, paths, clock, only),
        partial(
            _guide_claude_association,
            paths=paths,
            clock=clock,
            input_stream=sys.stdin,
            output=sys.stdout,
            error_output=sys.stderr,
        ),
    )


def _run_dashboard_loop(
    launch: Callable[[], DashboardApplicationResult],
    associate: Callable[[ClaudeAssociationRequest], None],
) -> int:
    """Rebuild a closed dashboard after each guided association result."""
    while True:
        result = launch()
        if isinstance(result, int):
            return result
        associate(result)


def _run_dashboard_once(
    paths: ApplicationPaths,
    clock: Clock,
    only: ProviderId | None,
) -> DashboardApplicationResult:
    """Build and run one fresh dashboard session."""
    snapshots = CachedDashboardSnapshotSource(paths, clock)
    capabilities = build_provider_capability_service(paths, os.environ)
    lookup = UsageLookupWorkerClient(
        UsageLookupModuleLaunchPlanner(
            resolve_usage_lookup_interpreter(),
            os.environ,
        )
    )
    session = InteractiveDashboardSession(
        snapshots.load(only),
        snapshots=snapshots,
        only=only,
        lookup=lookup,
        connector=_connect_dashboard_control,
        socket_path=paths.supervisor_socket,
        setup=GuidedServiceSetup(
            build_daemon_manager(
                claude_launcher=partial(
                    resolve_claude_launcher,
                    os.environ,
                ),
                codex_launcher=partial(
                    resolve_codex_launcher,
                    os.environ,
                ),
                paths=paths,
                clock=clock,
                provider_readiness=capabilities,
            ),
            ServiceSetupAcknowledgementStore(
                paths.service_setup_acknowledgement
            ),
        ),
        environment=os.environ,
    )
    return InteractiveDashboardApplication(session).run()


def _guide_claude_association(
    request: ClaudeAssociationRequest,
    *,
    paths: ApplicationPaths,
    clock: Clock,
    input_stream: TextIO,
    output: TextIO,
    error_output: TextIO,
) -> None:
    """Confirm and run one stable-ID private Claude association."""
    try:
        account = next(
            (
                candidate
                for candidate in AccountIndexReader(paths.accounts).load()
                if candidate.account_id == request.account_id
            ),
            None,
        )
    except PersistenceError:
        error_output.write(f"{CLAUDE_ASSOCIATION_READ_FAILURE}\n")
        error_output.flush()
        return
    if account is None or not isinstance(
        account.authority,
        ClaudeAccountAuthority,
    ):
        return
    authority = account.authority
    if authority.setup_token is None or authority.subscription is not None:
        return
    output.write(
        CLAUDE_ASSOCIATION_PROMPT.format(
            label=sanitize_terminal_text(account.label)
        )
    )
    output.flush()
    answer = input_stream.readline()
    if answer.strip().casefold() not in CLAUDE_ASSOCIATION_APPROVALS:
        return
    try:
        owner = compose_claude_migration(paths=paths, clock=clock)
    except ApplicationCompositionError as error:
        error_output.write(f"{error.failure.message}\n")
        error_output.flush()
        return
    try:
        result = owner.value.migrate_account(
            request.account_id,
            establish_identity=True,
            interactive=True,
        )
    finally:
        owner.close()
    if isinstance(result, ProviderFailure):
        error_output.write(f"{result.message}\n")
        error_output.flush()


if __name__ == "__main__":
    sys.exit(main())

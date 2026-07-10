"""Command-line entry point.

Typer-based CLI. Each subcommand is a top-level function decorated
with ``@app.command()``. State lives in a lazily initialized
:class:`AppContext`; tests inject fakes through :func:`set_context`.
"""

import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Annotated, NoReturn, Protocol, assert_never

import click
import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from sidekick_usages import __version__
from sidekick_usages.branding import (
    PROVIDER_COLORS,
    brand_header,
    update_status_line,
)
from sidekick_usages.cli_help import BrandedTyper, BrandedTyperGroup
from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.core.models import (
    Account,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials import (
    CredentialService,
    LocalCredentialSource,
    TokenCredentialSource,
)
from sidekick_usages.daemon import DaemonManager, DaemonOperation
from sidekick_usages.doctor import (
    DoctorService,
    render_doctor,
)
from sidekick_usages.doctor import (
    doctor_exit_code as account_doctor_exit_code,
)
from sidekick_usages.errors import (
    ForbiddenError,
    UnsupportedOperationError,
    UsageError,
)
from sidekick_usages.heartbeat import (
    HeartbeatOutcome,
    HeartbeatProvider,
    HeartbeatService,
    heartbeat_exit_code,
    heartbeat_supported_label,
    render_heartbeat_outcomes,
    render_heartbeat_status,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.lifetime import (
    LifetimeFailure,
    LifetimeResult,
    claude_lifetime_output,
    codex_lifetime_output,
)
from sidekick_usages.maintenance import (
    RefreshOutcome,
    TokenMaintenanceService,
    refresh_exit_code,
)
from sidekick_usages.paths import (
    PrivateCodexLocations,
    discover_application_paths,
)
from sidekick_usages.persistence.account_store import (
    AccountStore,
    AccountStoreStateError,
)
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceCompositionFailure,
    PersistenceOperationResult,
    operation_exit_code,
    recovery_guidance,
    recovery_next_command,
)
from sidekick_usages.persistence.assessment import (
    doctor_exit_code as persistence_doctor_exit_code,
)
from sidekick_usages.persistence.errors import (
    ManagedFileReadError,
    PersistenceCode,
    PersistenceError,
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.inventory import OrphanedPrivateCredentials
from sidekick_usages.persistence.migration_errors import (
    PersistenceMigrationStateError,
    PrototypeReimportRequiredError,
    SchedulerMutationBlockedError,
)
from sidekick_usages.persistence.migrations import (
    PermissionRepairOperationResult,
    PersistenceMigrationService,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.v060 import ReleasedV060Verifier
from sidekick_usages.providers.base import (
    Provider,
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude import (
    ClaudeProvider,
    SetupTokenMissing,
    SetupTokenRejected,
    SetupTokenSuccess,
    SetupTokenTimedOut,
    SetupTokenUnreadable,
)
from sidekick_usages.providers.registry import (
    build_heartbeat_registry,
    build_provider_registry,
)
from sidekick_usages.render import usage_overview
from sidekick_usages.token_input import TokenInput
from sidekick_usages.update import (
    InstallMethod,
    detect_install_method,
    fetch_latest_release,
    is_newer,
    manual_instructions,
    upgrade_command_for,
)
from sidekick_usages.usage import UsageCheckService


# ---------------------------------------------------------------------
# App context: injectable state
# ---------------------------------------------------------------------
class PersistenceCommands(Protocol):
    """Persistence operations required by the current CLI adapter."""

    def assess(self) -> PersistenceAssessment:
        """Return a passive assessment."""

    def mutation_preview(self) -> PersistenceAssessment:
        """Require scheduler quiescence and return a safe preview."""

    def read_accounts(self) -> tuple[Account, ...]:
        """Return a validated read-only account snapshot."""

    def migrate_accounts(
        self,
        *,
        reimport_prototype: bool = False,
    ) -> PersistenceAssessment:
        """Migrate account authority or import the prototype."""

    def prepare_rollback(self) -> PersistenceOperationResult:
        """Prepare exact released-v0.6.0 compatibility."""

    def repair_permissions(self) -> PermissionRepairOperationResult:
        """Repair and verify the released-layout permission boundary."""

    def full_reset(self) -> PersistenceAssessment:
        """Delete every Sidekick-owned credential artifact."""


@dataclass
class AppContext:
    """Mutable container for shared dependencies.

    :ivar store: Account store (loaded lazily on first use).
    :ivar http: Shared HTTP client with retry/backoff.
    :ivar providers: Provider registry (mutable for tests).
    :ivar heartbeat_providers: Heartbeat provider registry (mutable for tests).
    :ivar private_codex_locations: Private Codex credential roots.
    :ivar lifetime_sources: Configured lifetime collectors by provider id.
    :ivar clock: Invocation-scoped application wall clock.
    :ivar console: Rich console for stdout.
    :ivar err_console: Rich console pinned to stderr.
    """

    store: AccountStore | None
    http: HttpClient
    providers: dict[ProviderId, Provider]
    heartbeat_providers: dict[ProviderId, HeartbeatProvider]
    private_codex_locations: PrivateCodexLocations
    lifetime_sources: dict[ProviderId, Callable[[], LifetimeResult]]
    console: Console
    err_console: Console
    clock: Clock
    credentials: CredentialService | None = None
    runtime_loader: (
        Callable[
            [],
            tuple[AccountStore, CredentialService, PersistenceAssessment],
        ]
        | None
    ) = None
    persistence: PersistenceCommands | None = None
    persistence_assessment: PersistenceAssessment | None = None
    persistence_failure: PersistenceCompositionFailure | None = None

    def require_store(self) -> AccountStore:
        """Return the loaded runtime store or surface its blocked state."""
        if self.store is not None:
            return self.store
        self._load_runtime()
        if self.store is not None:
            return self.store
        if self.persistence_assessment is not None:
            assessment = self.persistence_assessment
            self.err_console.print(f"[red]{assessment.message}[/red]")
            if assessment.next_command is not None:
                self.err_console.print(
                    "[dim]Next: "
                    + shlex.join(assessment.next_command)
                    + "[/dim]"
                )
            raise typer.Exit(
                code=persistence_doctor_exit_code(assessment.code)
            )
        if self.persistence_failure is not None:
            self._exit_persistence_failure()
        raise RuntimeError("Application context has no account store.")

    def require_persistence(self) -> PersistenceCommands:
        """Return the configured persistence coordinator."""
        if self.persistence is None:
            if self.persistence_failure is not None:
                self._exit_persistence_failure()
            raise RuntimeError(
                "Application context has no persistence service."
            )
        return self.persistence

    def require_credentials(self) -> CredentialService:
        """Return the configured credential application service."""
        if self.credentials is not None:
            return self.credentials
        self._load_runtime()
        if self.credentials is not None:
            return self.credentials
        if self.persistence_assessment is not None:
            self.require_store()
        raise RuntimeError("Application context has no credential service.")

    def _load_runtime(self) -> None:
        """Load the store and credential service as one composed unit."""
        loader = self.runtime_loader
        if loader is None:
            return
        try:
            store, credentials, assessment = loader()
        except AccountStoreStateError as error:
            self.persistence_assessment = error.assessment
            self.runtime_loader = None
            return
        self.store = store
        self.credentials = credentials
        self.persistence_assessment = assessment
        self.runtime_loader = None

    def _exit_persistence_failure(self) -> NoReturn:
        """Render and exit for a captured passive composition failure."""
        failure = self.persistence_failure
        if failure is None:
            raise RuntimeError("Application context has no failure.")
        self.err_console.print(f"[red]{failure.message}[/red]")
        self.err_console.print(f"[dim]Path: {failure.safe_path}[/dim]")
        if failure.artifact_basename is not None:
            self.err_console.print(
                f"[dim]Artifact: {failure.artifact_basename}[/dim]"
            )
        if failure.guidance is not None:
            self.err_console.print(f"[dim]{failure.guidance}[/dim]")
        if failure.next_command is not None:
            self.err_console.print(
                "[dim]Next: " + shlex.join(failure.next_command) + "[/dim]"
            )
        raise typer.Exit(code=persistence_doctor_exit_code(failure.code))


_SAFE_CODEX_CACHE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_CODEX_CACHE_DIGEST_HEX_LENGTH = 32
_CODEX_CACHE_STEM_LENGTH = 221
_MIN_TOKEN_LENGTH_FOR_MASKING = 30
_DAEMON_BACKEND_HELP = (
    "Scheduler backend: auto, systemd, cron, launchd, task-scheduler."
)
_RUNTIME_PERSISTENCE_CODES = frozenset(
    {
        PersistenceCode.EMPTY,
        PersistenceCode.CURRENT,
        PersistenceCode.PROTOTYPE_IMPORTED,
    }
)


def _composition_failure(
    error: ManagedFileReadError
    | UnsafeManagedFileError
    | UnsupportedFilesystemError,
    safe_path: Path,
) -> PersistenceCompositionFailure:
    """Create one consistent safe failure with bounded recovery guidance."""
    return PersistenceCompositionFailure(
        code=error.code,
        safe_path=safe_path,
        artifact_basename=error.artifact_basename,
        message=str(error),
        next_command=recovery_next_command(error.code),
        guidance=recovery_guidance(error.code),
    )


def _build_default_context() -> AppContext:
    """Construct the default production app context.

    :return: An :class:`AppContext` wired with real dependencies.
    """
    paths = discover_application_paths()
    clock = SystemClock()
    with ExitStack() as cleanup:
        http = cleanup.enter_context(HttpClient(clock=clock))
        persistence: PersistenceMigrationService | None = None
        assessment: PersistenceAssessment | None = None
        failure: PersistenceCompositionFailure | None = None
        runtime_loader: (
            Callable[
                [],
                tuple[AccountStore, CredentialService, PersistenceAssessment],
            ]
            | None
        ) = None
        private_credentials: PrivateCredentialTree | None = None
        providers = build_provider_registry(clock)
        try:
            private_credentials = PrivateCredentialTree(
                paths.private_codex.canonical,
                account_path=paths.accounts.canonical,
                existing_root=paths.private_codex.existing_sidekick,
            )
            daemon_manager = DaemonManager()
            persistence = PersistenceMigrationService(
                paths.accounts,
                scheduler_assessor=daemon_manager.assess_quiescence,
                private_credential_artifacts=private_credentials,
                released_v060_verifier=ReleasedV060Verifier(),
            )
            assessment = persistence.assess()
            interrupted_private = (
                assessment.code is PersistenceCode.INTERRUPTED_ARTIFACTS
                and private_credentials.observe()
                is OrphanedPrivateCredentials.INTERRUPTED
            )
            if (
                assessment.code in _RUNTIME_PERSISTENCE_CODES
                or interrupted_private
            ):

                def load_runtime() -> tuple[
                    AccountStore,
                    CredentialService,
                    PersistenceAssessment,
                ]:
                    store = AccountStore(
                        paths.accounts,
                        orphaned_credentials_observer=(
                            private_credentials.observe
                        ),
                        private_credentials=private_credentials,
                    ).load()
                    credentials = CredentialService(
                        store,
                        http,
                        providers,
                        private_credentials,
                        clock=clock,
                    )
                    return store, credentials, persistence.assess()

                runtime_loader = load_runtime
        except (
            ManagedFileReadError,
            UnsafeManagedFileError,
            UnsupportedFilesystemError,
        ) as error:
            safe_path = (
                paths.private_codex.canonical
                if private_credentials is None
                or error.artifact_basename
                == paths.private_codex.canonical.name
                else paths.accounts.canonical
            )
            failure = _composition_failure(error, safe_path)
        context = AppContext(
            store=None,
            http=http,
            providers=providers,
            heartbeat_providers=build_heartbeat_registry(providers),
            private_codex_locations=paths.private_codex,
            lifetime_sources={
                ProviderId.CLAUDE: claude_lifetime_output,
                ProviderId.CODEX: partial(
                    codex_lifetime_output,
                    paths.lifetime_cache_file,
                ),
            },
            console=Console(),
            err_console=Console(stderr=True),
            clock=clock,
            runtime_loader=runtime_loader,
            persistence=persistence,
            persistence_assessment=assessment,
            persistence_failure=failure,
        )
        cleanup.pop_all()
        return context


class _ContextState:
    """Holds the active app context as a class attribute.

    Mutating a class attribute avoids a module-level ``global``
    rebind (PLW0603) while preserving the same test-injection hook
    via :func:`set_context`.
    """

    ctx: AppContext | None = None
    only: ProviderId | None = None
    close_context: click.Context | None = None


def _get_ctx() -> AppContext:
    """Return the active app context, building one if needed.

    :return: The active :class:`AppContext`.
    """
    if _ContextState.ctx is None:
        _ContextState.ctx = _build_default_context()
    app_ctx = _ContextState.ctx
    current = click.get_current_context(silent=True)
    if current is not None:
        root = current.find_root()
        if _ContextState.close_context is not root:
            root.call_on_close(app_ctx.http.close)
            _ContextState.close_context = root
    return app_ctx


def set_context(ctx: AppContext) -> None:
    """Override the context (tests inject fakes via this hook).

    :param ctx: New context to use for subsequent commands.
    """
    _ContextState.ctx = ctx


# ---------------------------------------------------------------------
# Typer app and global options
# ---------------------------------------------------------------------
app = BrandedTyper(
    name="sidekick-usages",
    cls=BrandedTyperGroup,
    rich_markup_mode="rich",
    no_args_is_help=False,
    pretty_exceptions_show_locals=False,
    add_completion=False,
)

daemon_app = BrandedTyper(
    cls=BrandedTyperGroup,
    help="Install, inspect, or remove scheduled token refresh.",
    rich_markup_mode="rich",
)
app.add_typer(daemon_app, name="daemon")

migrate_app = BrandedTyper(
    cls=BrandedTyperGroup,
    help="Migrate account storage or prepare release rollback.",
    rich_markup_mode="rich",
)
app.add_typer(migrate_app, name="migrate")

permissions_app = BrandedTyper(
    cls=BrandedTyperGroup,
    help="Inspect or repair Sidekick-owned permissions.",
    rich_markup_mode="rich",
)
app.add_typer(permissions_app, name="permissions")


class _HeartbeatGroup(BrandedTyperGroup):
    """Treat an unknown heartbeat subcommand as an account label."""

    label_command_name = "run-label"

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        """Resolve known subcommands, falling back to heartbeat <label>."""
        try:
            return super().resolve_command(ctx, args)
        except click.UsageError:
            if args and not args[0].startswith("-"):
                command = self.get_command(ctx, self.label_command_name)
                if command is not None:
                    return self.label_command_name, command, args
            raise


heartbeat_app = BrandedTyper(
    cls=_HeartbeatGroup,
    help="Warm inactive usage windows for opted-in accounts.",
    rich_markup_mode="rich",
    invoke_without_command=True,
    context_settings={"allow_extra_args": True},
)
app.add_typer(heartbeat_app, name="heartbeat")


def _version_callback(value: bool) -> None:
    """Print the version and exit (``--version`` option callback).

    :param value: True when the flag was passed.
    """
    if value:
        typer.echo(f"sidekick-usages {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    only: Annotated[
        str | None,
        typer.Option(
            "--only",
            help="Filter to one provider's accounts.",
            metavar="PROVIDER",
        ),
    ] = None,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show version and exit.",
        ),
    ] = False,
) -> None:
    """Default invocation runs ``check`` if no subcommand is given."""
    try:
        provider_filter = ProviderId(only) if only is not None else None
    except ValueError:
        typer.echo(
            f"Unknown provider {only!r}. Known: "
            + ", ".join(provider.value for provider in ProviderId)
            + ".",
            err=True,
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None
    _ContextState.only = provider_filter
    if ctx.invoked_subcommand is not None:
        return
    _get_ctx()
    _run_usage_check()


# ---------------------------------------------------------------------
# check (default)
# ---------------------------------------------------------------------
@app.command("check")
def check_cmd() -> None:
    """Print usage for every saved account."""
    _run_usage_check()


def _run_usage_check() -> None:
    """Run the typed usage service and render its complete result.

    Account failures require manual action; lifetime collection failures
    produce a system-error exit after the completed state is rendered.
    """
    app_ctx = _get_ctx()
    provider_filter = _ContextState.only
    result = UsageCheckService(
        app_ctx.require_store(),
        app_ctx.http,
        app_ctx.providers,
        app_ctx.require_credentials(),
        clock=app_ctx.clock,
    ).check(provider_filter)
    if not result.usages and not result.failures:
        _print_no_accounts(provider_filter, branded=True)
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)

    provider_ids = {
        *(usage.provider_id for usage in result.usages),
        *(failure.provider_id for failure in result.failures),
    }
    lifetime = _lifetime_for(provider_ids, app_ctx.lifetime_sources)
    exit_code = ExitCode.MANUAL_ACTION if result.failures else ExitCode.SUCCESS
    if any(
        isinstance(result, LifetimeFailure) for result in lifetime.values()
    ):
        exit_code = _combined_exit_code(exit_code, ExitCode.SYSTEM_ERROR)

    app_ctx.console.print(
        usage_overview(
            result.usages,
            lifetime,
            failures=result.failures,
            width=app_ctx.console.size.width,
            reference_time=app_ctx.clock.now(),
        )
    )
    if exit_code:
        raise typer.Exit(code=exit_code)


def _lifetime_for(
    provider_ids: set[ProviderId],
    sources: dict[ProviderId, Callable[[], LifetimeResult]],
) -> dict[ProviderId, LifetimeResult]:
    """Collect lifetime once per provider represented by selected accounts."""
    return {
        provider_id: source()
        for provider_id, source in sources.items()
        if provider_id in provider_ids
    }


# ---------------------------------------------------------------------
# add
# ---------------------------------------------------------------------
@app.command("add")
def add_cmd(
    provider: Annotated[
        str,
        typer.Argument(
            help="Provider id (claude or codex).",
        ),
    ],
    label: Annotated[
        str | None,
        typer.Option(
            "--label",
            help="Override the auto-generated label.",
        ),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option(
            "--token",
            help="Paste a token instead of auto-detecting.",
        ),
    ] = None,
    plan: Annotated[
        str | None,
        typer.Option(
            "--plan",
            help="Override the auto-detected plan tag.",
        ),
    ] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help=(
                "Read Codex credentials from this source CODEX_HOME, "
                "then copy them into sidekick's private cache."
            ),
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing label.",
        ),
    ] = False,
) -> None:
    """Save an account. Idempotent: same token reuses the entry.

    Auto-detects credentials from the local provider install when
    ``--token`` is omitted. Falls back to a hidden prompt (or stdin
    if piped) when no local login is found.
    """
    app_ctx = _get_ctx()
    prov = _resolve_provider(provider)
    if codex_home is not None and prov.id is not ProviderId.CODEX:
        _usage_error("--codex-home can only be used with the codex provider.")
    source = (
        TokenCredentialSource(provider_id=prov.id, token=token)
        if token is not None
        else LocalCredentialSource(
            provider_id=prov.id,
            credential_home=(
                codex_home.expanduser() if codex_home is not None else None
            ),
        )
    )
    target_label = _validated_label(label) if label is not None else None
    result = app_ctx.require_credentials().save(
        source,
        label=target_label,
        plan=plan,
        force=force,
    )
    if (
        isinstance(result, ProviderFailure)
        and token is None
        and result.kind is ProviderFailureKind.MISSING
    ):
        prompted = _prompt_for_token(prov)
        if not prompted:
            app_ctx.err_console.print(
                "[red]No valid token provided. Cancelled.[/red]"
            )
            raise typer.Exit(code=ExitCode.MANUAL_ACTION)
        source_name = "stdin" if not sys.stdin.isatty() else "prompt"
        app_ctx.console.print(f"[green]Got token from {source_name}.[/green]")
        result = app_ctx.require_credentials().save(
            TokenCredentialSource(provider_id=prov.id, token=prompted),
            label=target_label,
            plan=plan,
            force=force,
        )
    if isinstance(result, ProviderFailure):
        _exit_credential_failure(result)
    action = "Saved" if result.created else "Updated"
    app_ctx.console.print(f"[green]{action} '{result.label}'.[/green]")
    if result.warning is not None:
        app_ctx.console.print(f"[yellow]Note: {result.warning}[/yellow]")


# ---------------------------------------------------------------------
# list
# ---------------------------------------------------------------------
@app.command("list")
def list_cmd() -> None:
    """List every saved account."""
    app_ctx = _get_ctx()
    app_ctx.console.print(
        brand_header(
            app_ctx.console.size.width,
            section="saved accounts",
        )
    )
    accounts = list(app_ctx.require_store())
    if not accounts:
        app_ctx.console.print("[dim](no accounts saved)[/dim]")
        return

    table = Table(
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 2),
        pad_edge=False,
    )
    table.add_column("Label", no_wrap=True)
    table.add_column("Provider", no_wrap=True)
    table.add_column("Plan", no_wrap=True)
    table.add_column("Heartbeat", no_wrap=True)
    table.add_column("Token", no_wrap=True, style="dim")

    for acct in accounts:
        heartbeat_provider = app_ctx.heartbeat_providers.get(acct.provider_id)
        prov_color = PROVIDER_COLORS.get(acct.provider_id, "dim")
        plan_text = (
            Text(acct.plan, style="dim")
            if acct.plan == "unknown"
            else Text(acct.plan)
        )
        table.add_row(
            acct.label,
            Text(acct.provider_id, style=prov_color),
            plan_text,
            heartbeat_supported_label(acct, heartbeat_provider),
            _masked_token(acct.access_token),
        )
    app_ctx.console.print(table)
    app_ctx.console.print(
        f"\n[dim]Config: {app_ctx.require_store().path}[/dim]"
    )


# ---------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------
@app.command("remove")
def remove_cmd(
    label: Annotated[
        str,
        typer.Argument(help="Account label to delete."),
    ],
) -> None:
    """Delete a saved account."""
    app_ctx = _get_ctx()
    if not app_ctx.require_store().remove_credentials(label):
        app_ctx.err_console.print(
            f"[yellow]No account named '{label}'.[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    app_ctx.console.print(f"[green]Removed '{label}'.[/green]")


# ---------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------
@app.command("rename")
def rename_cmd(
    old: Annotated[str, typer.Argument(help="Existing label.")],
    new: Annotated[str, typer.Argument(help="New label.")],
) -> None:
    """Rename a saved account."""
    app_ctx = _get_ctx()
    if not app_ctx.require_store().rename(old, new):
        app_ctx.err_console.print(
            f"[yellow]Cannot rename: '{old}' is missing or "
            f"'{new}' already exists.[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    app_ctx.console.print(f"[green]Renamed '{old}' → '{new}'.[/green]")


# ---------------------------------------------------------------------
# set-plan
# ---------------------------------------------------------------------
@app.command("set-plan")
def set_plan_cmd(label: str, plan: str) -> None:
    """Manually set an account's plan tag.

    For credentials the usage API cannot introspect (e.g. inference-
    only Claude tokens), this is the supported way to correct the
    plan chip.
    """
    app_ctx = _get_ctx()
    value = plan.strip().lower()
    if not value:
        app_ctx.err_console.print("[red]Plan must not be empty.[/red]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    store = app_ctx.require_store()
    acct = store.get(label)
    if acct is None:
        app_ctx.err_console.print(f"[red]No account labeled '{label}'.[/red]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    acct.plan = value
    app_ctx.require_store().persist(acct)
    app_ctx.console.print(
        f"Set [bold]{label}[/bold] plan to [bold]{value}[/bold]."
    )


# ---------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------
@app.command("refresh")
def refresh_cmd(
    label: Annotated[
        str | None,
        typer.Argument(help="Account label."),
    ] = None,
    all_accounts: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Refresh every due account using saved refresh tokens.",
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            help="Only print accounts needing manual action.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="With --all, refresh even if tokens are still fresh.",
        ),
    ] = False,
    from_codex_home: Annotated[
        Path | None,
        typer.Option(
            "--from-codex-home",
            help=(
                "Read Codex credentials from this CODEX_HOME instead "
                "of the saved/default home."
            ),
        ),
    ] = None,
    replace_identity: Annotated[
        bool,
        typer.Option(
            "--replace-identity",
            help=(
                "Allow replacing the saved provider account id with the "
                "current local login."
            ),
        ),
    ] = False,
) -> None:
    """Replace a saved account's token with the local CLI login.

    With a label, reads the current login from the provider's local
    install and writes the new access token into that saved account.
    With ``--all``, uses only saved refresh tokens and never adopts
    the current global provider login.
    """
    label = _validate_refresh_args(
        label,
        all_accounts=all_accounts,
        quiet=quiet,
        force=force,
        from_codex_home=from_codex_home,
        replace_identity=replace_identity,
    )
    if all_accounts:
        _refresh_all_cmd(quiet=quiet, force=force)
        return
    if label is None:
        raise AssertionError("refresh label validation failed")
    app_ctx = _get_ctx()
    store = app_ctx.require_store()
    acct = store.get(label)
    if acct is None:
        app_ctx.err_console.print(
            f"[yellow]No account named '{label}'.[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    if (
        from_codex_home is not None
        and acct.provider_id is not ProviderId.CODEX
    ):
        _usage_error("--from-codex-home requires a saved Codex account.")
    result = app_ctx.require_credentials().refresh_from_source(
        label,
        LocalCredentialSource(
            provider_id=acct.provider_id,
            credential_home=(
                from_codex_home.expanduser()
                if from_codex_home is not None
                else None
            ),
        ),
        replace_identity=replace_identity,
    )
    if isinstance(result, ProviderFailure):
        _exit_credential_failure(result)
    app_ctx.console.print(f"[green]Updated token for '{label}'.[/green]")


def _validate_refresh_args(
    label: str | None,
    *,
    all_accounts: bool,
    quiet: bool,
    force: bool,
    from_codex_home: Path | None,
    replace_identity: bool,
) -> str | None:
    """Validate refresh command mode and return a narrowed label."""
    if all_accounts:
        if label is not None:
            _usage_error("--all cannot be combined with an account label.")
        if from_codex_home is not None:
            _usage_error("--from-codex-home only applies to a label refresh.")
        if replace_identity:
            _usage_error("--replace-identity only applies to a label refresh.")
        return None
    if label is None:
        _usage_error("Pass an account label or use --all.")
    if quiet:
        _usage_error("--quiet only applies with --all.")
    if force:
        _usage_error("--force only applies with --all.")
    return label


def _token_maintenance_service(
    app_ctx: AppContext,
) -> TokenMaintenanceService:
    """Build token maintenance from the active command dependencies."""
    return TokenMaintenanceService(
        app_ctx.require_store(),
        app_ctx.require_credentials(),
        clock=app_ctx.clock,
    )


def _refresh_all_cmd(*, quiet: bool, force: bool) -> None:
    """Run scheduler-safe saved-token refresh for all accounts."""
    app_ctx = _get_ctx()
    accounts = list(app_ctx.require_store())
    if not accounts:
        _print_no_accounts(None)
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    service = _token_maintenance_service(app_ctx)
    outcomes = service.refresh_all(force=force)
    for outcome in outcomes:
        if quiet and outcome.exit_code is ExitCode.SUCCESS:
            continue
        if outcome.status is RefreshStatus.OK:
            app_ctx.console.print(f"[green]{outcome.label}: refreshed[/green]")
        elif outcome.status is RefreshStatus.FAILED:
            app_ctx.console.print(
                f"[red]{outcome.label}: {outcome.message}[/red]"
            )
        elif not quiet:
            app_ctx.console.print(
                f"[dim]{outcome.label}: skipped ({outcome.message})[/dim]"
            )
    code = refresh_exit_code(outcomes)
    if code:
        raise typer.Exit(code=code)


# ---------------------------------------------------------------------
# heartbeat / maintain
# ---------------------------------------------------------------------
@heartbeat_app.callback()
def heartbeat_cmd(
    ctx: typer.Context,
    all_accounts: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Warm every enabled account with an inactive 5h window.",
        ),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option(
            "--quiet",
            help="Only print accounts needing manual action.",
        ),
    ] = False,
    provider_id: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="With --all, filter to one provider.",
        ),
    ] = None,
    target_id: Annotated[
        str | None,
        typer.Option(
            "--target",
            help="Heartbeat target to warm: standard, spark, or all.",
        ),
    ] = None,
) -> None:
    """Warm inactive usage windows without changing token refresh policy."""
    if ctx.invoked_subcommand is not None:
        return
    args = list(ctx.args)
    label = args[0] if args else None
    if len(args) > 1:
        _usage_error("Pass at most one account label.")
    if all_accounts:
        try:
            provider_filter = (
                ProviderId(provider_id) if provider_id is not None else None
            )
        except ValueError:
            _usage_error(f"Unknown provider {provider_id!r}.")
        outcomes = _heartbeat_service().heartbeat_all(
            provider_id=provider_filter,
            target_id=target_id,
        )
        _render_heartbeat_outcomes(outcomes, quiet=quiet)
        code = heartbeat_exit_code(outcomes)
        if code:
            raise typer.Exit(code=code)
        return
    if provider_id is not None:
        _usage_error("--provider only applies with --all.")
    if quiet:
        _usage_error("--quiet only applies with --all.")
    if label is None:
        _usage_error("Pass an account label or use --all.")
    _run_heartbeat_label(label, target_id=target_id)


@heartbeat_app.command("run-label", hidden=True)
def heartbeat_label_cmd(
    label: Annotated[str, typer.Argument(help="Account label to warm.")],
    target_id: Annotated[
        str | None,
        typer.Option(
            "--target",
            help="Heartbeat target to warm: standard, spark, or all.",
        ),
    ] = None,
) -> None:
    """Hidden target for the heartbeat <label> fallback parser."""
    _run_heartbeat_label(label, target_id=target_id)


def _run_heartbeat_label(label: str, *, target_id: str | None = None) -> None:
    """Run a one-shot heartbeat for one account label."""
    app_ctx = _get_ctx()
    account = app_ctx.require_store().get(label)
    if account is None:
        app_ctx.err_console.print(
            f"[yellow]No account named '{label}'.[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    outcome = _heartbeat_service().heartbeat_account(
        account,
        require_enabled=False,
        target_id=target_id,
    )
    _render_heartbeat_outcomes([outcome], quiet=False)
    if outcome.exit_code:
        raise typer.Exit(code=outcome.exit_code)


@heartbeat_app.command("enable")
def heartbeat_enable_cmd(
    label: Annotated[str, typer.Argument(help="Account label to enable.")],
    target_id: Annotated[
        str | None,
        typer.Option(
            "--target",
            help="Daemon target to enable: standard, spark, or all.",
        ),
    ] = None,
) -> None:
    """Enable daemon heartbeat for one supported account."""
    app_ctx = _get_ctx()
    outcome = _heartbeat_service().enable(
        app_ctx.require_store().get(label),
        target_id=target_id,
    )
    _render_heartbeat_outcomes([outcome], quiet=False)
    if outcome.exit_code:
        raise typer.Exit(code=outcome.exit_code)


@heartbeat_app.command("disable")
def heartbeat_disable_cmd(
    label: Annotated[str, typer.Argument(help="Account label to disable.")],
    target_id: Annotated[
        str | None,
        typer.Option(
            "--target",
            help="Daemon target to disable: standard, spark, or all.",
        ),
    ] = None,
) -> None:
    """Disable daemon heartbeat for one account."""
    app_ctx = _get_ctx()
    outcome = _heartbeat_service().disable(
        app_ctx.require_store().get(label),
        target_id=target_id,
    )
    _render_heartbeat_outcomes([outcome], quiet=False)
    if outcome.exit_code:
        raise typer.Exit(code=outcome.exit_code)


@heartbeat_app.command("status")
def heartbeat_status_cmd(
    provider_id: Annotated[
        str | None,
        typer.Option("--provider", help="Filter to one provider."),
    ] = None,
    label: Annotated[
        str | None,
        typer.Option("--label", help="Filter to one account label."),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit machine-readable JSON."),
    ] = False,
) -> None:
    """Show heartbeat support and latest diagnostics."""
    app_ctx = _get_ctx()
    accounts = list(app_ctx.require_store())
    if provider_id is not None:
        accounts = [a for a in accounts if a.provider_id == provider_id]
    if label is not None:
        accounts = [a for a in accounts if a.label == label]
    if not accounts:
        app_ctx.err_console.print("[yellow]No matching accounts.[/yellow]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    render_heartbeat_status(
        accounts,
        app_ctx.heartbeat_providers,
        app_ctx.console,
        json_output=json_output,
    )


@app.command("maintain")
def maintain_cmd(
    quiet: Annotated[
        bool,
        typer.Option("--quiet", help="Only print manual-action failures."),
    ] = False,
) -> None:
    """Run scheduler-safe token refresh, then opted-in heartbeat."""
    app_ctx = _get_ctx()
    accounts = list(app_ctx.require_store())
    if not accounts:
        _print_no_accounts(None)
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)

    refresh_service = _token_maintenance_service(app_ctx)
    refresh_outcomes = refresh_service.refresh_all()
    _render_refresh_outcomes(refresh_outcomes, quiet=quiet)

    heartbeat_outcomes = HeartbeatService(
        app_ctx.require_store(),
        app_ctx.http,
        app_ctx.heartbeat_providers,
        clock=app_ctx.clock,
    ).heartbeat_all()
    _render_heartbeat_outcomes(heartbeat_outcomes, quiet=quiet)

    code = _combined_exit_code(
        refresh_exit_code(refresh_outcomes),
        heartbeat_exit_code(heartbeat_outcomes),
    )
    if code:
        raise typer.Exit(code=code)


def _render_refresh_outcomes(
    outcomes: list[RefreshOutcome],
    *,
    quiet: bool,
) -> None:
    """Render refresh outcomes using existing command wording."""
    app_ctx = _get_ctx()
    for outcome in outcomes:
        if quiet and outcome.exit_code is ExitCode.SUCCESS:
            continue
        if outcome.status is RefreshStatus.OK:
            app_ctx.console.print(f"[green]{outcome.label}: refreshed[/green]")
        elif outcome.status is RefreshStatus.FAILED:
            app_ctx.console.print(
                f"[red]{outcome.label}: {outcome.message}[/red]"
            )
        elif not quiet:
            app_ctx.console.print(
                f"[dim]{outcome.label}: skipped ({outcome.message})[/dim]"
            )


def _render_heartbeat_outcomes(
    outcomes: list[HeartbeatOutcome],
    *,
    quiet: bool,
) -> None:
    """Render heartbeat outcomes for manual or scheduled runs."""
    app_ctx = _get_ctx()
    render_heartbeat_outcomes(
        outcomes,
        console=app_ctx.console,
        err_console=app_ctx.err_console,
        quiet=quiet,
    )


def _heartbeat_service() -> HeartbeatService:
    """Build a heartbeat service from the active app context."""
    app_ctx = _get_ctx()
    return HeartbeatService(
        app_ctx.require_store(),
        app_ctx.http,
        app_ctx.heartbeat_providers,
        clock=app_ctx.clock,
    )


def _combined_exit_code(left: ExitCode, right: ExitCode) -> ExitCode:
    """Return the highest-priority maintenance exit code."""
    exit_codes = (left, right)
    if ExitCode.SCHEDULER_ERROR in exit_codes:
        return ExitCode.SCHEDULER_ERROR
    if ExitCode.SYSTEM_ERROR in exit_codes:
        return ExitCode.SYSTEM_ERROR
    if ExitCode.MANUAL_ACTION in exit_codes:
        return ExitCode.MANUAL_ACTION
    return ExitCode.SUCCESS


def _usage_error(message: str) -> NoReturn:
    """Print a CLI usage error and exit."""
    app_ctx = _get_ctx()
    app_ctx.err_console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=ExitCode.SYSTEM_ERROR)


# ---------------------------------------------------------------------
# migrate
# ---------------------------------------------------------------------
@migrate_app.command("accounts")
def migrate_accounts_cmd(
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the interactive confirmation."),
    ] = False,
    reimport_prototype: Annotated[
        bool,
        typer.Option(
            "--reimport-prototype",
            help="Replace current state from a changed prototype.",
        ),
    ] = False,
) -> None:
    """Explicitly migrate account storage to the current schema."""
    app_ctx = _get_ctx()
    service = app_ctx.require_persistence()
    try:
        assessment = service.mutation_preview()
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        _exit_persistence_error(error)
    _render_persistence_preview(
        assessment,
        title="Account migration",
        detail=(
            "Changed prototype replacement is explicitly enabled."
            if reimport_prototype
            else None
        ),
    )
    _confirm_persistence_operation(yes)
    try:
        result = service.migrate_accounts(
            reimport_prototype=reimport_prototype
        )
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        _exit_persistence_error(error)
    _render_persistence_success("Migration complete", result)


@migrate_app.command("prepare-rollback")
def prepare_rollback_cmd(
    target: Annotated[
        str,
        typer.Option(
            "--target",
            help="Released compatibility target (v0.6.0).",
        ),
    ],
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the interactive confirmation."),
    ] = False,
) -> None:
    """Prepare exact compatibility with released version 0.6.0."""
    app_ctx = _get_ctx()
    if target != "v0.6.0":
        app_ctx.err_console.print(
            "[red]Unsupported rollback target. Expected 'v0.6.0'.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    service = app_ctx.require_persistence()
    try:
        assessment = service.mutation_preview()
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        _exit_persistence_error(error)
    _render_persistence_preview(
        assessment,
        title="Prepare rollback to v0.6.0",
    )
    _confirm_persistence_operation(yes)
    try:
        result = service.prepare_rollback()
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        _exit_persistence_error(error)
    _render_persistence_operation_success("Rollback prepared", result)


@permissions_app.command("repair")
def repair_permissions_cmd(
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Skip the interactive confirmation."),
    ] = False,
) -> None:
    """Repair a validated released-layout permission boundary."""
    app_ctx = _get_ctx()
    service = app_ctx.require_persistence()
    try:
        preview: PersistenceAssessment | PersistenceCompositionFailure = (
            service.mutation_preview()
        )
    except UnsafeManagedFileError as error:
        safe_path = (
            app_ctx.private_codex_locations.canonical
            if error.artifact_basename
            == app_ctx.private_codex_locations.canonical.name
            else app_ctx.persistence_assessment.safe_path
            if app_ctx.persistence_assessment is not None
            else app_ctx.private_codex_locations.canonical.parent
            / "accounts.json"
        )
        preview = _composition_failure(error, safe_path)
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        _exit_persistence_error(error)
    _render_persistence_preview(preview, title="Permission repair")
    _confirm_persistence_operation(yes)
    try:
        result = service.repair_permissions()
    except (SchedulerMutationBlockedError, PersistenceError) as error:
        _exit_persistence_error(error)
    app_ctx.console.print(
        "[green]Permissions repaired.[/green] " + result.assessment.message
    )
    app_ctx.console.print(
        "[dim]Application parent changed: "
        f"{'yes' if result.repair.account_parent_repaired else 'no'}; "
        f"private directories changed: {result.repair.directories_repaired}; "
        f"private files changed: {result.repair.files_repaired}.[/dim]"
    )


def _render_persistence_preview(
    assessment: PersistenceAssessment | PersistenceCompositionFailure,
    *,
    title: str,
    detail: str | None = None,
) -> None:
    """Render bounded, credential-free persistence mutation details."""
    if isinstance(assessment, PersistenceAssessment):
        generation = assessment.generation
        count = (
            str(assessment.account_count)
            if assessment.account_count is not None
            else "unknown"
        )
    else:
        generation = "unknown"
        count = "unknown"
    lines = [
        f"State: {assessment.code}",
        f"Generation: {generation}",
        f"Validated accounts: {count}",
        f"Path: {assessment.safe_path}",
    ]
    if assessment.artifact_basename is not None:
        lines.append(f"Artifact: {assessment.artifact_basename}")
    if detail is not None:
        lines.append(detail)
    _get_ctx().console.print(
        Panel(
            Text("\n".join(lines)),
            border_style="yellow",
            title=f"[yellow]{title}[/yellow]",
            title_align="left",
        )
    )


def _confirm_persistence_operation(yes: bool) -> None:
    """Require explicit confirmation unless non-interactive intent exists."""
    if yes:
        return
    app_ctx = _get_ctx()
    if Confirm.ask("Continue?", default=False, console=app_ctx.console):
        return
    app_ctx.console.print("Cancelled.")
    raise typer.Exit(code=ExitCode.MANUAL_ACTION)


def _render_persistence_success(
    action: str,
    assessment: PersistenceAssessment,
) -> None:
    """Render a safe successful assessment."""
    _get_ctx().console.print(f"[green]{action}.[/green] {assessment.message}")


def _render_persistence_operation_success(
    action: str,
    result: PersistenceOperationResult,
) -> None:
    """Render a safe successful operation and optional artifact."""
    _get_ctx().console.print(f"[green]{action}.[/green] {result.message}")
    if result.artifact_basename is not None:
        _get_ctx().console.print(
            f"[dim]Snapshot: {result.artifact_basename}[/dim]"
        )


def _exit_persistence_error(
    error: SchedulerMutationBlockedError | PersistenceError,
) -> NoReturn:
    """Render a typed safe failure using the stable process vocabulary."""
    app_ctx = _get_ctx()
    app_ctx.err_console.print(f"[red]{error}[/red]")
    if isinstance(error, SchedulerMutationBlockedError):
        for observation in error.assessment.observations:
            app_ctx.err_console.print(
                f"[dim]{observation.backend}: {observation.state} — "
                f"{observation.message}[/dim]"
            )
        raise typer.Exit(code=ExitCode.SCHEDULER_ERROR)
    if (
        isinstance(
            error,
            PersistenceMigrationStateError | PrototypeReimportRequiredError,
        )
        and error.next_command is not None
    ):
        app_ctx.err_console.print(
            "[dim]Next: " + shlex.join(error.next_command) + "[/dim]"
        )
    raise typer.Exit(code=_persistence_error_exit_code(error))


def _persistence_error_exit_code(error: PersistenceError) -> ExitCode:
    """Map any persistence failure without ever treating it as success."""
    try:
        code = operation_exit_code(error.code)
    except ValueError:
        code = persistence_doctor_exit_code(error.code)
    return ExitCode.MANUAL_ACTION if code is ExitCode.SUCCESS else code


# ---------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------
def _render_persistence_doctor(
    app_ctx: AppContext,
    *,
    json_output: bool,
) -> None:
    """Render the context's persistence-only doctor result."""
    failure = app_ctx.persistence_failure
    assessment = app_ctx.persistence_assessment
    if failure is not None:
        render_doctor(
            [],
            app_ctx.console,
            json_output=json_output,
            persistence_failure=failure,
        )
        code = persistence_doctor_exit_code(failure.code)
    elif assessment is not None:
        render_doctor(
            [],
            app_ctx.console,
            json_output=json_output,
            persistence=assessment,
        )
        code = persistence_doctor_exit_code(assessment.code)
    else:
        raise RuntimeError("Doctor has no persistence result.")
    if code:
        raise typer.Exit(code=code)


def _doctor_accounts(
    app_ctx: AppContext,
    *,
    json_output: bool,
) -> tuple[Account, ...]:
    """Read doctor accounts without constructing the mutable runtime store."""
    if app_ctx.store is not None:
        return tuple(app_ctx.store)
    assessment = app_ctx.persistence_assessment
    if (
        app_ctx.persistence_failure is not None
        or assessment is None
        or assessment.code not in _RUNTIME_PERSISTENCE_CODES
    ):
        _render_persistence_doctor(app_ctx, json_output=json_output)
        return ()
    try:
        return app_ctx.require_persistence().read_accounts()
    except PersistenceMigrationStateError as error:
        render_doctor(
            [],
            app_ctx.console,
            json_output=json_output,
            persistence=error.assessment,
        )
        raise typer.Exit(
            code=persistence_doctor_exit_code(error.assessment.code)
        ) from None
    except (
        ManagedFileReadError,
        UnsafeManagedFileError,
        UnsupportedFilesystemError,
    ) as error:
        safe_path = (
            app_ctx.private_codex_locations.canonical
            if error.artifact_basename
            == app_ctx.private_codex_locations.canonical.name
            else assessment.safe_path
        )
        failure = _composition_failure(error, safe_path)
        render_doctor(
            [],
            app_ctx.console,
            json_output=json_output,
            persistence_failure=failure,
        )
        raise typer.Exit(
            code=persistence_doctor_exit_code(failure.code)
        ) from None


@app.command("doctor")
def doctor_cmd(
    provider_id: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Filter diagnostics to one provider.",
        ),
    ] = None,
    label: Annotated[
        str | None,
        typer.Option(
            "--label",
            help="Filter diagnostics to one saved account label.",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit machine-readable JSON diagnostics.",
        ),
    ] = False,
) -> None:
    """Report what is healthy and what needs login."""
    app_ctx = _get_ctx()
    try:
        provider_filter = (
            ProviderId(provider_id) if provider_id is not None else None
        )
    except ValueError:
        app_ctx.err_console.print(
            f"[red]Unknown provider {provider_id!r}.[/red]"
        )
        raise typer.Exit(code=ExitCode.SYSTEM_ERROR) from None
    assessment = app_ctx.persistence_assessment
    accounts = _doctor_accounts(app_ctx, json_output=json_output)
    service = DoctorService(
        accounts,
        app_ctx.providers,
        app_ctx.heartbeat_providers,
        clock=app_ctx.clock,
    )
    diagnostics = service.diagnostics(provider_id=provider_filter, label=label)
    if not diagnostics:
        if assessment is not None and assessment.account_count == 0:
            _render_persistence_doctor(app_ctx, json_output=json_output)
            return
        app_ctx.err_console.print("[yellow]No matching accounts.[/yellow]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    render_doctor(
        diagnostics,
        app_ctx.console,
        json_output=json_output,
        persistence=assessment,
    )
    code = account_doctor_exit_code(diagnostics)
    if assessment is not None:
        code = _combined_exit_code(
            code,
            persistence_doctor_exit_code(assessment.code),
        )
    if code:
        raise typer.Exit(code=code)


# ---------------------------------------------------------------------
# daemon
# ---------------------------------------------------------------------
@daemon_app.command("install")
def daemon_install_cmd(
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            help=_DAEMON_BACKEND_HELP,
        ),
    ] = "auto",
) -> None:
    """Install scheduled saved-token refresh for the current user."""
    _run_daemon_operation(DaemonOperation.INSTALL, backend)


@daemon_app.command("status")
def daemon_status_cmd(
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            help=_DAEMON_BACKEND_HELP,
        ),
    ] = "auto",
) -> None:
    """Inspect scheduled saved-token refresh for the current user."""
    _run_daemon_operation(DaemonOperation.STATUS, backend)


@daemon_app.command("uninstall")
def daemon_uninstall_cmd(
    backend: Annotated[
        str,
        typer.Option(
            "--backend",
            help=_DAEMON_BACKEND_HELP,
        ),
    ] = "auto",
) -> None:
    """Remove scheduled saved-token refresh for the current user."""
    _run_daemon_operation(DaemonOperation.UNINSTALL, backend)


def _run_daemon_operation(
    operation: DaemonOperation,
    backend: str,
) -> None:
    """Run one daemon manager operation and render its result."""
    app_ctx = _get_ctx()
    manager = DaemonManager()
    try:
        result = manager.run(operation, backend)
    except (UsageError, ValueError) as e:
        app_ctx.err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=ExitCode.SCHEDULER_ERROR) from e
    style = "green" if result.exit_code is ExitCode.SUCCESS else "red"
    if (
        operation is DaemonOperation.STATUS
        and result.exit_code is ExitCode.SUCCESS
    ):
        app_ctx.console.print(
            brand_header(
                app_ctx.console.size.width,
                section="daemon status",
            )
        )
    app_ctx.console.print(
        f"[{style}]{result.backend}: {result.message}[/{style}]"
    )
    if result.exit_code:
        raise typer.Exit(code=result.exit_code)


@app.command("codex-login")
def codex_login_cmd(
    label: Annotated[str, typer.Argument(help="Account label to update.")],
    codex_home: Annotated[
        Path | None,
        typer.Option(
            "--codex-home",
            help=(
                "Advanced: run login against this source CODEX_HOME "
                "before importing a private sidekick copy."
            ),
        ),
    ] = None,
    device_auth: Annotated[
        bool,
        typer.Option(
            "--device-auth",
            help="Use Codex CLI device authentication.",
        ),
    ] = False,
    replace_identity: Annotated[
        bool,
        typer.Option(
            "--replace-identity",
            help=(
                "Allow replacing the saved provider account id with the "
                "login from this Codex home."
            ),
        ),
    ] = False,
) -> None:
    """Run ``codex login`` and import a private sidekick auth copy."""
    app_ctx = _get_ctx()
    result = app_ctx.require_credentials().login_codex(
        _validated_label(label),
        source_home=(
            codex_home.expanduser() if codex_home is not None else None
        ),
        device_auth=device_auth,
        replace_identity=replace_identity,
    )
    if isinstance(result, ProviderFailure):
        _exit_credential_failure(result)
    app_ctx.console.print(f"[green]Updated Codex login for '{label}'.[/green]")


@app.command("codex-export")
def codex_export_cmd(
    label: Annotated[str, typer.Argument(help="Saved Codex account label.")],
    codex_home: Annotated[
        Path,
        typer.Option(
            "--codex-home",
            help="Target isolated Codex CODEX_HOME.",
        ),
    ],
    source_codex_home: Annotated[
        Path | None,
        typer.Option(
            "--source-codex-home",
            help=(
                "Optional source CODEX_HOME whose auth.json belongs to "
                "this account."
            ),
        ),
    ] = None,
) -> None:
    """Export a saved Codex account into a file-backed Codex home."""
    app_ctx = _get_ctx()
    exported = app_ctx.require_credentials().export_codex(
        label,
        codex_home,
        source_home=source_codex_home,
    )
    if isinstance(exported, ProviderFailure):
        _exit_credential_failure(exported, prefix=f"Cannot export '{label}': ")

    app_ctx.console.print(
        f"[green]Exported '{label}' to Codex home "
        f"{exported.target_home}.[/green]"
    )


# ---------------------------------------------------------------------
# setup-token
# ---------------------------------------------------------------------
def _run_setup_token(provider: Provider) -> str | None:
    """Render one structured Claude setup-token outcome."""
    if not isinstance(provider, ClaudeProvider):
        raise UnsupportedOperationError(
            "Codex CLI doesn't expose a long-lived token generator. "
            "Run `codex login` then `sidekick-usages add codex`."
        )
    app_ctx = _get_ctx()
    app_ctx.err_console.print(
        "[dim]Running `claude setup-token` — complete the browser OAuth "
        "flow when it opens...[/dim]"
    )
    result = provider.capture_setup_token()
    if isinstance(result, SetupTokenTimedOut):
        app_ctx.err_console.print("[red]`claude setup-token` timed out.[/red]")
        return None
    if isinstance(result, SetupTokenSuccess):
        return result.token
    if isinstance(result, SetupTokenMissing):
        app_ctx.err_console.print(
            "[red]Claude setup completed without returning a token.[/red]"
        )
        return None
    if isinstance(result, SetupTokenRejected):
        app_ctx.err_console.print(
            "[red]`claude setup-token` did not complete successfully "
            f"(exit {result.return_code}).[/red]"
        )
        return None
    if isinstance(result, SetupTokenUnreadable):
        app_ctx.err_console.print(
            "[red]`claude setup-token` could not be completed safely.[/red]"
        )
        return None
    assert_never(result)


@app.command("setup-token")
def setup_token_cmd(
    provider: Annotated[
        str,
        typer.Argument(help="Provider id (currently: claude)."),
    ],
    label: Annotated[
        str | None,
        typer.Option(
            "--label",
            help="Override the auto-generated label.",
        ),
    ] = None,
    plan: Annotated[
        str | None,
        typer.Option(
            "--plan",
            help="Override the plan tag.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite an existing label.",
        ),
    ] = False,
) -> None:
    """Run a provider's long-lived token generator.

    Currently only Claude Code supports this (``claude setup-token``
    generates a one-year token). Codex CLI does not have an
    equivalent — use ``add codex`` after ``codex login`` instead.
    """
    app_ctx = _get_ctx()
    prov = _resolve_provider(provider)
    try:
        token = _run_setup_token(prov)
    except UnsupportedOperationError as e:
        app_ctx.err_console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from e

    if not token:
        app_ctx.err_console.print(
            f"[red]Did not capture a token. Try again or run "
            f"`sidekick-usages add {prov.id}` with --token."
            f"[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)

    result = app_ctx.require_credentials().save(
        TokenCredentialSource(provider_id=prov.id, token=token),
        label=_validated_label(label) if label is not None else None,
        plan=plan,
        force=force,
    )
    if isinstance(result, ProviderFailure):
        _exit_credential_failure(result)
    action = "Saved" if result.created else "Updated"
    app_ctx.console.print(f"[green]{action} '{result.label}'.[/green]")
    if result.warning is not None:
        app_ctx.console.print(f"[yellow]Note: {result.warning}[/yellow]")


# ---------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------
def _reset_provider_accounts(
    app_ctx: AppContext,
    provider_id: ProviderId,
    provider: str,
) -> None:
    """Reset one provider through the typed persistence boundary."""
    try:
        cleared = app_ctx.require_store().reset_provider_credentials(
            provider_id
        )
    except PersistenceError as error:
        _exit_persistence_error(error)
    app_ctx.console.print(
        f"[green]Cleared {cleared} {provider} account(s).[/green]"
    )


@app.command("reset")
def reset_cmd(
    yes: Annotated[
        bool,
        typer.Option(
            "-y",
            "--yes",
            help="Skip confirmation prompt.",
        ),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Only reset one provider's accounts.",
        ),
    ] = None,
) -> None:
    """Delete saved accounts (all, or one provider).

    Prompts for confirmation unless ``--yes`` is passed.
    """
    app_ctx = _get_ctx()
    provider_id: ProviderId | None = None
    validated_count: int | None = None
    if provider:
        try:
            provider_id = ProviderId(provider)
        except ValueError:
            app_ctx.err_console.print(
                f"[red]Unknown provider {provider!r}.[/red]"
            )
            raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None
        targets = app_ctx.require_store().filter_by_provider(provider_id)
        count = len(targets)
        scope = f"{count} {provider} account(s)"
    else:
        service = app_ctx.require_persistence()
        try:
            assessment = service.mutation_preview()
        except (SchedulerMutationBlockedError, PersistenceError) as error:
            _exit_persistence_error(error)
        validated_count = assessment.account_count
        count = validated_count or 0
        scope = (
            f"{count} validated account(s) and all managed credential "
            f"artifacts at {assessment.safe_path}"
        )
    if count == 0 and provider_id is not None:
        app_ctx.console.print("[dim]Nothing to reset.[/dim]")
        return

    if not yes:
        app_ctx.console.print(
            Panel(
                f"This will delete {scope}.",
                border_style="yellow",
                title="[yellow]Confirm reset[/yellow]",
                title_align="left",
            )
        )
        if not Confirm.ask(
            "Continue?",
            default=False,
            console=app_ctx.console,
        ):
            app_ctx.console.print("Cancelled.")
            raise typer.Exit(code=ExitCode.MANUAL_ACTION)

    if provider_id is not None:
        _reset_provider_accounts(app_ctx, provider_id, provider or "")
    else:
        cleared = count
        try:
            app_ctx.require_persistence().full_reset()
        except (SchedulerMutationBlockedError, PersistenceError) as error:
            _exit_persistence_error(error)
        if validated_count is None:
            app_ctx.console.print(
                "[green]Cleared all managed account and credential "
                "artifacts.[/green]"
            )
        else:
            app_ctx.console.print(
                f"[green]Cleared {cleared} account(s) and removed "
                f"config file.[/green]"
            )


# ---------------------------------------------------------------------
# check-update / update
# ---------------------------------------------------------------------
@app.command("check-update")
def check_update_cmd() -> None:
    """Check whether a newer release is available on GitHub."""
    app_ctx = _get_ctx()
    try:
        latest = fetch_latest_release(app_ctx.http)
    except ForbiddenError as e:
        app_ctx.err_console.print(
            "[yellow]GitHub rate limit reached; try again later.[/yellow]"
        )
        if e.api_message:
            app_ctx.err_console.print(f"[dim]{e.api_message}[/dim]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None
    except UsageError as e:
        app_ctx.err_console.print(f"[red]Could not check: {e}[/red]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None
    except ValueError as e:
        app_ctx.err_console.print(
            f"[red]Unexpected GitHub response: {e}[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None

    app_ctx.console.print(update_status_line())
    app_ctx.console.print()
    if is_newer(latest, __version__):
        app_ctx.console.print(
            f"[green]New version {latest} available[/green] "
            f"(currently {__version__}). "
            "Run [bold]sidekick-usages update[/bold] to upgrade."
        )
    else:
        app_ctx.console.print(f"[dim]Up to date ({__version__}).[/dim]")


@app.command("update")
def update_cmd(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Print the upgrade command without running it.",
        ),
    ] = False,
) -> None:
    """Upgrade sidekick-usages to the latest release.

    Detects the install method from ``sys.executable`` and invokes
    the matching upgrade command. Refuses to guess when the install
    method can't be determined — falls back to manual instructions.
    """
    app_ctx = _get_ctx()
    method = detect_install_method()
    if method is InstallMethod.UNKNOWN:
        app_ctx.err_console.print(f"[yellow]{manual_instructions()}[/yellow]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)

    argv = upgrade_command_for(method)
    app_ctx.console.print(f"[dim]$ {' '.join(argv)}[/dim]")
    if dry_run:
        return

    try:
        subprocess.run(argv, check=True)
    except FileNotFoundError as e:
        app_ctx.err_console.print(
            f"[red]Upgrade tool {argv[0]!r} not found on PATH.[/red] "
            f"Install {argv[0]!r} and retry, or run a different "
            "upgrade path manually."
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from e
    except subprocess.CalledProcessError as e:
        raise typer.Exit(code=e.returncode) from e


# ---------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------
def _resolve_provider(provider_id: str) -> Provider:
    """Resolve a provider id, raising a Typer exit on miss.

    :param provider_id: Provider id from user input.
    :return: The matching :class:`Provider`.
    """
    app_ctx = _get_ctx()
    try:
        resolved_id = ProviderId(provider_id)
    except ValueError:
        resolved_id = None
    provider = (
        app_ctx.providers.get(resolved_id) if resolved_id is not None else None
    )
    if provider is None:
        app_ctx.err_console.print(
            f"[red]Unknown provider {provider_id!r}. "
            f"Known: {', '.join(sorted(app_ctx.providers))}.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    return provider


def _exit_credential_failure(
    failure: ProviderFailure,
    *,
    prefix: str = "",
) -> NoReturn:
    """Render one secret-safe credential failure and exit."""
    _get_ctx().err_console.print(f"[red]{prefix}{failure.message}[/red]")
    raise typer.Exit(code=ExitCode.MANUAL_ACTION)


def _prompt_for_token(provider: Provider) -> str | None:
    """Show provider-specific hints, then collect a token.

    :param provider: Provider whose token format to validate.
    :return: A validated token, or ``None`` on cancel/garbage.
    """
    app_ctx = _get_ctx()
    if not sys.stdin.isatty():
        app_ctx.console.print(
            f"[dim]No local {provider.display_name} login "
            f"found — reading token from stdin...[/dim]"
        )
    else:
        app_ctx.console.print(
            f"[dim]No local {provider.display_name} login found. "
            f"Paste an OAuth token (input hidden), or press Ctrl-C "
            f"to cancel.[/dim]"
        )
        if provider.id is ProviderId.CLAUDE:
            app_ctx.console.print(
                "[dim]Tip: run `sidekick-usages setup-token "
                "claude` to generate one.[/dim]"
            )
    ti = TokenInput(provider.token_pattern)
    return ti.read()


def _validated_label(value: str) -> AccountLabel:
    """Validate one CLI label and translate failure to CLI vocabulary."""
    try:
        return AccountLabel(value)
    except ValueError as error:
        _get_ctx().err_console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from error


def _masked_token(token: str) -> str:
    """Return a display-safe partial access-token mask."""
    if len(token) <= _MIN_TOKEN_LENGTH_FOR_MASKING:
        return "(missing)"
    return token[:18] + "…" + token[-6:]


# ---------------------------------------------------------------------
# Error rendering
# ---------------------------------------------------------------------
def _print_no_accounts(
    only: ProviderId | None,
    *,
    branded: bool = False,
) -> None:
    """Print the 'no accounts saved' hint.

    :param only: Provider filter that produced no results.
    :param branded: Whether to prepend the interactive application header.
    """
    app_ctx = _get_ctx()
    if branded:
        app_ctx.err_console.print(brand_header(app_ctx.err_console.size.width))
        app_ctx.err_console.print()
    scope = f" for {only}" if only else ""
    app_ctx.err_console.print(
        Panel(
            Text.from_markup(
                f"No accounts saved{scope}.\n\n"
                f"Run [bold]sidekick-usages add <provider>[/bold] "
                f"after logging into the CLI."
            ),
            border_style="yellow",
            title="[yellow]Nothing to show[/yellow]",
            title_align="left",
        )
    )


# ---------------------------------------------------------------------
# Entry-point wrapping for argv overrides + exception conversion
# ---------------------------------------------------------------------
def _run_typer(argv: list[str] | None = None) -> int:
    """Invoke Typer and convert typed application failures to process codes.

    :param argv: Optional explicit command arguments for an embedded caller.
    :return: Process exit code.
    """
    try:
        result: object = app(args=argv, standalone_mode=False)
        if isinstance(result, int):
            return int(result)
        return 0
    except typer.Exit as e:
        return int(e.exit_code or 0)
    except PersistenceError as e:
        Console(stderr=True).print(f"[red]{e}[/red]")
        return int(_persistence_error_exit_code(e))
    except UsageError as e:
        Console(stderr=True).print(f"[red]{e}[/red]")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return 130


if __name__ == "__main__":
    sys.exit(_run_typer())

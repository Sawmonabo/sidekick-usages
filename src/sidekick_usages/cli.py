"""Command-line entry point.

Typer-based CLI. Each subcommand is a top-level function decorated
with ``@app.command()``. State lives in a lazily initialized
:class:`AppContext`; tests inject fakes through :func:`set_context`.
"""

import hashlib
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime
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
from sidekick_usages.core.expiry import (
    InvalidExpiry,
    UnknownExpiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
    DetectedCredentials,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ProviderId,
    RefreshStatus,
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
    AuthError,
    ForbiddenError,
    RateLimitError,
    TransientError,
    UnsupportedOperationError,
    UsageError,
)
from sidekick_usages.heartbeat import (
    HeartbeatOutcome,
    HeartbeatProvider,
    HeartbeatService,
    build_heartbeat_registry,
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
    record_refresh_success,
    refresh_exit_code,
)
from sidekick_usages.paths import (
    PrivateCodexLocations,
    discover_application_paths,
)
from sidekick_usages.persistence.account_store import AccountStore
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
from sidekick_usages.providers import build_provider_registry
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.claude import (
    ClaudeProvider,
    SetupTokenMissing,
    SetupTokenSuccess,
    SetupTokenTimedOut,
)
from sidekick_usages.providers.codex import (
    CodexProvider,
    auth_blob_matches_account,
    default_codex_home,
    ensure_file_auth_home,
    read_auth_blob,
    write_account_auth_file,
    write_private_account_auth_bundle,
)
from sidekick_usages.render import usage_overview
from sidekick_usages.serialization import JsonObject
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
    store_loader: Callable[[], AccountStore] | None = None
    private_credentials: PrivateCredentialTree | None = None
    persistence: PersistenceCommands | None = None
    persistence_assessment: PersistenceAssessment | None = None
    persistence_failure: PersistenceCompositionFailure | None = None

    def require_store(self) -> AccountStore:
        """Return the loaded runtime store or surface its blocked state."""
        if self.store is not None:
            return self.store
        if self.store_loader is not None:
            store = self.store_loader()
            self.store = store
            self.store_loader = None
            return store
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

    def require_private_credentials(self) -> PrivateCredentialTree:
        """Return the shared-lock private credential boundary."""
        if self.private_credentials is None:
            store = self.require_store()
            self.private_credentials = PrivateCredentialTree(
                self.private_codex_locations.canonical,
                account_path=store.path,
                existing_root=self.private_codex_locations.existing_sidekick,
            )
        return self.private_credentials

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
        store_loader: Callable[[], AccountStore] | None = None
        private_credentials: PrivateCredentialTree | None = None
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
            if assessment.code in _RUNTIME_PERSISTENCE_CODES:

                def load_store() -> AccountStore:
                    return AccountStore(
                        paths.accounts,
                        orphaned_credentials_observer=(
                            private_credentials.observe
                        ),
                    ).load()

                store_loader = load_store
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
        providers = build_provider_registry(clock, private_credentials)
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
            store_loader=store_loader,
            private_credentials=private_credentials,
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


#: Scope required to read the OAuth usage endpoint. Matches the
#: ``gLH`` constant in the Claude Code binary; the in-tree ``hT()``
#: predicate gates ``/api/oauth/usage`` on whether the stored
#: credentials' ``scopes`` array contains exactly this string.
_USAGE_REQUIRED_SCOPE = "user:profile"


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

    normalized_codex_home = _normalize_codex_home(prov, codex_home)
    detected: DetectedCredentials | None = None

    if not token:
        detected = prov.detect_credentials(normalized_codex_home)
        if detected:
            token = detected.access_token
            if not plan:
                plan = detected.plan
            app_ctx.console.print(
                f"[green]Detected token (plan: {plan}) from local "
                f"{prov.display_name} login.[/green]"
            )
        else:
            token = _prompt_for_token(prov)
            if not token:
                app_ctx.err_console.print(
                    "[red]No valid token provided. Cancelled.[/red]"
                )
                raise typer.Exit(code=ExitCode.MANUAL_ACTION)
            src = "stdin" if not sys.stdin.isatty() else "prompt"
            app_ctx.console.print(f"[green]Got token from {src}.[/green]")

    if detected is None:
        detected = _manual_credentials(prov.id, token)
    if isinstance(detected.expiry, InvalidExpiry):
        app_ctx.err_console.print(
            "[red]Detected credential expiry metadata is invalid.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    existing = app_ctx.require_store().find_by_token(prov.id, token)
    reference_time = app_ctx.clock.now()
    if existing is not None:
        _upsert_existing(
            existing,
            label,
            plan,
            force,
            detected=detected,
            source_codex_home=normalized_codex_home,
            reference_time=reference_time,
        )
        return
    _insert_new(
        prov,
        detected,
        label,
        plan,
        force,
        source_codex_home=normalized_codex_home,
        reference_time=reference_time,
    )


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
    if not app_ctx.require_store().remove(label):
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
    provider = app_ctx.providers.get(acct.provider_id)
    if provider is None:
        app_ctx.err_console.print(
            f"[red]Unknown provider '{acct.provider_id}' for '{label}'.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    credential_home = _refresh_credential_home(
        provider,
        from_codex_home,
    )
    detected = provider.detect_credentials(credential_home)
    if not detected:
        app_ctx.err_console.print(
            f"[red]No {provider.display_name} token found "
            f"locally. Run the appropriate login command first."
            f"[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    _ensure_refresh_identity_matches(
        acct,
        detected,
        label,
        replace_identity=replace_identity,
    )
    reference_time = app_ctx.clock.now()
    _apply_detected_credentials(
        acct,
        detected,
        provider,
        credential_home,
        app_ctx.private_codex_locations,
        reference_time=reference_time,
    )
    record_refresh_success(acct, reference_time)
    app_ctx.require_store().persist(acct)
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
        app_ctx.http,
        app_ctx.providers,
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


def _ensure_refresh_identity_matches(
    acct: Account,
    detected: DetectedCredentials,
    label: str,
    *,
    replace_identity: bool,
) -> None:
    """Reject refresh when the active login is a different account.

    :param acct: Saved account being refreshed.
    :param detected: Credentials detected from the local provider login.
    :param label: User-facing account label.
    :param replace_identity: Whether the user explicitly allowed an
        account-id replacement.
    :raises typer.Exit: When both sides expose account ids and they differ.
    """
    saved_id = acct.provider_account_id
    detected_id = detected.provider_account_id
    if replace_identity or saved_id is None or detected_id is None:
        return
    if saved_id == detected_id:
        return
    app_ctx = _get_ctx()
    app_ctx.err_console.print(
        "[red]Refusing to refresh "
        f"'{label}' with a different provider account.[/red]\n"
        f"  Saved account id:   {saved_id}\n"
        f"  Current login id:   {detected_id}\n"
        "  Log into the matching provider account, or rerun with "
        "--replace-identity to intentionally replace this label."
    )
    raise typer.Exit(code=ExitCode.MANUAL_ACTION)


# ---------------------------------------------------------------------
# codex-login / codex-export
# ---------------------------------------------------------------------
def _run_codex_login_command(
    source_home: Path | None,
    *,
    device_auth: bool,
) -> None:
    """Run Codex login without changing the provider's global home."""
    if source_home is not None:
        ensure_file_auth_home(source_home)
    argv = ["codex", "login"]
    if device_auth:
        argv.append("--device-auth")
    try:
        if source_home is None:
            subprocess.run(argv, check=True)
        else:
            environment = os.environ.copy()
            environment["CODEX_HOME"] = str(source_home)
            subprocess.run(argv, check=True, env=environment)
    except FileNotFoundError as error:
        _get_ctx().err_console.print(
            "[red]Codex CLI executable 'codex' was not found on PATH.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from error
    except subprocess.CalledProcessError as error:
        raise typer.Exit(code=error.returncode) from error


def _codex_login_account(
    store: AccountStore,
    label: str,
    detected: DetectedCredentials,
    *,
    replace_identity: bool,
) -> tuple[Account, bool]:
    """Resolve or initialize the account targeted by Codex login."""
    account = store.get(label)
    if account is not None and account.provider_id is not ProviderId.CODEX:
        _get_ctx().err_console.print(
            f"[red]'{label}' is a {account.provider_id} account, not "
            "codex.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    if account is None:
        if not isinstance(detected.credentials, CodexCredentials):
            _get_ctx().err_console.print(
                "[red]Codex returned incompatible credentials.[/red]"
            )
            raise typer.Exit(code=ExitCode.SYSTEM_ERROR)
        return (
            Account(
                label=AccountLabel(label),
                credentials=detected.credentials,
            ),
            True,
        )
    _ensure_refresh_identity_matches(
        account,
        detected,
        label,
        replace_identity=replace_identity,
    )
    return account, False


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
    provider = _require_codex_provider()
    source_home = codex_home.expanduser() if codex_home is not None else None
    _run_codex_login_command(source_home, device_auth=device_auth)
    detected = provider.detect_credentials(source_home)
    if detected is None:
        source = source_home or default_codex_home()
        app_ctx.err_console.print(
            f"[red]Codex login finished, but no auth.json was found in "
            f"{source}.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)

    store = app_ctx.require_store()
    acct, is_new = _codex_login_account(
        store,
        label,
        detected,
        replace_identity=replace_identity,
    )
    if is_new:
        store.persist(acct)
    reference_time = app_ctx.clock.now()
    _apply_detected_credentials(
        acct,
        detected,
        provider,
        source_home,
        app_ctx.private_codex_locations,
        reference_time=reference_time,
    )
    store.persist(acct)
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
    provider = _require_codex_provider()
    acct = app_ctx.require_store().get(label)
    if acct is None:
        app_ctx.err_console.print(
            f"[yellow]No account named '{label}'.[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    if acct.provider_id is not ProviderId.CODEX:
        app_ctx.err_console.print(
            f"[red]'{label}' is a {acct.provider_id} account, not codex.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    reference_time = app_ctx.clock.now()

    source_blob = _matching_codex_auth_blob(
        acct,
        source_codex_home,
        Path(acct.codex_home).expanduser() if acct.codex_home else None,
        default_codex_home(),
    )
    if source_blob is not None:
        _apply_matching_codex_blob(
            acct,
            source_blob,
            provider,
            app_ctx.private_codex_locations,
            reference_time=reference_time,
        )
    elif not acct.codex_id_token and acct.refresh_token:
        outcome = _token_maintenance_service(app_ctx).refresh_account(
            acct,
            force=True,
        )
        if not outcome.refreshed:
            app_ctx.err_console.print(
                f"[red]Could not refresh '{label}': {outcome.message}[/red]"
            )
            raise typer.Exit(code=outcome.exit_code)

    if not write_account_auth_file(
        acct,
        codex_home.expanduser(),
        reference_time=reference_time,
        source_blob=source_blob,
    ):
        app_ctx.err_console.print(
            "[red]Cannot export a complete Codex auth file for "
            f"'{label}'.[/red]\n"
            "  Missing Codex id_token or account id metadata. Run "
            f"`sidekick-usages codex-login {label}` once for that account."
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)

    app_ctx.require_store().persist(acct)
    app_ctx.console.print(
        f"[green]Exported '{label}' to Codex home {codex_home}.[/green]"
    )


# ---------------------------------------------------------------------
# setup-token
# ---------------------------------------------------------------------
def _run_setup_token(provider: Provider) -> str | None:
    """Run provider capture while keeping terminal output in the CLI."""
    if not isinstance(provider, ClaudeProvider):
        return provider.run_setup_token()
    app_ctx = _get_ctx()
    app_ctx.err_console.print(
        "[dim]Running `claude setup-token` — complete the browser OAuth "
        "flow when it opens...[/dim]"
    )
    result = provider.capture_setup_token()
    if isinstance(result, SetupTokenTimedOut):
        app_ctx.err_console.print("[red]`claude setup-token` timed out.[/red]")
        return None
    for line in result.output_lines:
        app_ctx.err_console.print(line, highlight=False)
    if isinstance(result, SetupTokenSuccess):
        return result.token
    if isinstance(result, SetupTokenMissing):
        if result.return_code != 0:
            app_ctx.err_console.print(
                "[red]`claude setup-token` exited with code "
                f"{result.return_code}.[/red]"
            )
        else:
            app_ctx.err_console.print(
                "[red]Could not find a token in the output of "
                "`claude setup-token`.[/red]"
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

    detected = _manual_credentials(prov.id, token)
    existing = app_ctx.require_store().find_by_token(prov.id, token)
    reference_time = app_ctx.clock.now()
    if existing is not None:
        _upsert_existing(
            existing,
            label,
            plan,
            force,
            reference_time=reference_time,
        )
        return
    _insert_new(
        prov,
        detected,
        label,
        plan,
        force,
        source_codex_home=None,
        reference_time=reference_time,
    )


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
        cleared = app_ctx.require_store().reset_provider(provider_id)
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
def _normalize_codex_home(
    provider: Provider,
    codex_home: Path | None,
) -> Path | None:
    """Validate and normalize a Codex home option."""
    if codex_home is None:
        return None
    if provider.id is not ProviderId.CODEX:
        app_ctx = _get_ctx()
        app_ctx.err_console.print(
            "[red]--codex-home can only be used with the codex provider.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    return codex_home.expanduser()


def _sidekick_codex_home(
    locations: PrivateCodexLocations,
    label: str,
) -> Path:
    """Return sidekick's private Codex auth cache dir for a label."""
    safe = _SAFE_CODEX_CACHE_NAME_RE.sub("_", label).strip("._-")
    if not safe:
        safe = "account"
    digest = hashlib.sha256(label.encode()).hexdigest()
    return locations.canonical / (
        f"{safe[:_CODEX_CACHE_STEM_LENGTH]}--"
        f"{digest[:_CODEX_CACHE_DIGEST_HEX_LENGTH]}"
    )


def _codex_source_blob(
    provider: Provider,
    source_home: Path | None,
) -> JsonObject | None:
    """Read a Codex source auth blob only for the real provider."""
    if isinstance(provider, CodexProvider):
        return read_auth_blob(source_home)
    return None


def _refresh_credential_home(
    provider: Provider,
    from_codex_home: Path | None,
) -> Path | None:
    """Pick the credential home for a manual refresh."""
    if from_codex_home is not None:
        return _normalize_codex_home(provider, from_codex_home)
    return None


def _apply_detected_credentials(
    acct: Account,
    detected: DetectedCredentials,
    provider: Provider,
    credential_home: Path | None,
    private_codex_locations: PrivateCodexLocations,
    *,
    reference_time: datetime,
) -> None:
    """Copy detected local credentials onto a saved account."""
    if acct.provider_id is not detected.provider_id:
        raise UsageError("Detected credentials belong to another provider.")
    if isinstance(detected.expiry, InvalidExpiry):
        raise UsageError("Detected credential expiry metadata is invalid.")
    if isinstance(acct.credentials, ClaudeCredentials) and isinstance(
        detected.credentials,
        ClaudeCredentials,
    ):
        incoming = detected.credentials
        acct.credentials = replace(
            acct.credentials,
            access_token=incoming.access_token,
            refresh_token=(
                incoming.refresh_token
                if incoming.refresh_token is not None
                else acct.credentials.refresh_token
            ),
            expiry=(
                incoming.expiry
                if not isinstance(incoming.expiry, UnknownExpiry)
                else acct.credentials.expiry
            ),
            scopes=(
                incoming.scopes
                if incoming.scopes is not None
                else acct.credentials.scopes
            ),
        )
    elif isinstance(acct.credentials, CodexCredentials) and isinstance(
        detected.credentials,
        CodexCredentials,
    ):
        incoming = detected.credentials
        acct.credentials = replace(
            acct.credentials,
            access_token=incoming.access_token,
            refresh_token=(
                incoming.refresh_token
                if incoming.refresh_token is not None
                else acct.credentials.refresh_token
            ),
            expiry=(
                incoming.expiry
                if not isinstance(incoming.expiry, UnknownExpiry)
                else acct.credentials.expiry
            ),
            account_id=incoming.account_id or acct.credentials.account_id,
            id_token=incoming.id_token or acct.credentials.id_token,
            auth_last_refresh=(
                incoming.auth_last_refresh
                or acct.credentials.auth_last_refresh
            ),
        )
    else:
        raise UsageError("Detected credentials are provider-incompatible.")
    if detected.plan and detected.plan != "unknown":
        acct.plan = detected.plan
    if provider.id is ProviderId.CODEX:
        _write_sidekick_codex_cache(
            acct,
            provider,
            credential_home,
            private_codex_locations,
            reference_time=reference_time,
        )


def _write_sidekick_codex_cache(
    acct: Account,
    provider: Provider,
    source_home: Path | None,
    private_codex_locations: PrivateCodexLocations,
    *,
    reference_time: datetime,
) -> bool:
    """Write sidekick's private copy of a Codex auth bundle."""
    if provider.id is not ProviderId.CODEX:
        return False
    return write_private_account_auth_bundle(
        acct,
        _get_ctx().require_private_credentials(),
        _sidekick_codex_home(private_codex_locations, acct.label),
        reference_time=reference_time,
        source_blob=_codex_source_blob(provider, source_home),
    )


def _require_codex_provider() -> Provider:
    """Return the configured Codex provider or exit."""
    app_ctx = _get_ctx()
    provider = app_ctx.providers.get(ProviderId.CODEX)
    if provider is None:
        app_ctx.err_console.print(
            "[red]Codex provider is not registered.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    return provider


def _matching_codex_auth_blob(
    acct: Account,
    *homes: Path | None,
) -> JsonObject | None:
    """Find a source Codex auth.json that belongs to ``acct``."""
    seen: set[str] = set()
    for home in homes:
        if home is None:
            continue
        normalized = home.expanduser()
        key = str(normalized)
        if key in seen:
            continue
        seen.add(key)
        blob = read_auth_blob(normalized)
        if blob is not None and auth_blob_matches_account(blob, acct):
            return blob
    return None


def _apply_matching_codex_blob(
    acct: Account,
    blob: JsonObject,
    provider: Provider,
    private_codex_locations: PrivateCodexLocations,
    *,
    reference_time: datetime,
) -> None:
    """Apply metadata from a matching Codex auth blob to ``acct``."""
    detected = CodexProvider._parse_blob(blob)
    if detected is not None:
        _apply_detected_credentials(
            acct,
            detected,
            provider,
            None,
            private_codex_locations,
            reference_time=reference_time,
        )


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


def _manual_credentials(
    provider_id: ProviderId,
    token: str,
) -> DetectedCredentials:
    """Build the smallest honest credential result for a pasted token."""
    if provider_id is ProviderId.CLAUDE:
        credentials = ClaudeCredentials(access_token=token)
    else:
        credentials = CodexCredentials(access_token=token)
    return DetectedCredentials(credentials=credentials)


def _validated_label(value: str) -> AccountLabel:
    """Validate one CLI label and translate failure to CLI vocabulary."""
    try:
        return AccountLabel(value)
    except ValueError as error:
        _get_ctx().err_console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from error


def _claude_credentials(account: Account) -> ClaudeCredentials:
    """Return Claude credentials or reject a caller/provider mismatch."""
    credentials = account.credentials
    if isinstance(credentials, ClaudeCredentials):
        return credentials
    raise UsageError(f"Account {account.label!r} is not a Claude account.")


def _masked_token(token: str) -> str:
    """Return a display-safe partial access-token mask."""
    if len(token) <= _MIN_TOKEN_LENGTH_FOR_MASKING:
        return "(missing)"
    return token[:18] + "…" + token[-6:]


def _upsert_existing(
    existing: Account,
    label_override: str | None,
    plan: str | None,
    force: bool,
    *,
    detected: DetectedCredentials | None = None,
    source_codex_home: Path | None = None,
    reference_time: datetime,
) -> None:
    """Idempotent path: token already saved.

    :param existing: The account that already holds this token.
    :param label_override: New label requested by the user, if any.
    :param plan: Plan to apply, if any.
    :param force: Overwrite an existing target label.
    :param detected: Detected credential metadata, when available.
    :param source_codex_home: Optional source Codex auth home.
    :param reference_time: Aware time shared by credential file writes.
    """
    app_ctx = _get_ctx()
    store = app_ctx.require_store()
    target = (
        _validated_label(label_override)
        if label_override is not None
        else existing.label
    )
    if target != existing.label:
        if target in store and not force:
            app_ctx.err_console.print(
                f"[yellow]Token already saved as "
                f"'{existing.label}', but '{target}' already "
                f"exists too. Use --force to overwrite.[/yellow]"
            )
            raise typer.Exit(code=ExitCode.MANUAL_ACTION)
        store.rename(existing.label, target)
    acct = store.get(target)
    if acct is not None:
        if plan:
            acct.plan = plan
        if detected is not None:
            provider = _resolve_provider(acct.provider_id.value)
            _apply_detected_credentials(
                acct,
                detected,
                provider,
                source_codex_home,
                app_ctx.private_codex_locations,
                reference_time=reference_time,
            )
        store.persist(acct)
    app_ctx.console.print(
        f"[green]Token already saved as '{target}' — updated in place.[/green]"
    )


def _new_account_warning(account: Account, provider: Provider) -> str | None:
    """Validate a new account and return a non-blocking safe warning."""
    app_ctx = _get_ctx()
    try:
        provider.fetch_usage(account, app_ctx.http)
    except AuthError as error:
        app_ctx.err_console.print(
            "[red]Token rejected by API (HTTP 401).[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from error
    except ForbiddenError as error:
        if (
            error.required_scope == _USAGE_REQUIRED_SCOPE
            and account.scopes is None
        ):
            credentials = _claude_credentials(account)
            account.credentials = replace(credentials, scopes=())
            try:
                provider.fetch_usage(account, app_ctx.http)
            except UsageError as retry_error:
                return (
                    "Token saved, but the header probe also failed: "
                    f"{retry_error}"
                )
        else:
            _print_forbidden(provider, error)
    except RateLimitError as error:
        wait = (
            f"retry in {error.retry_after}s."
            if error.retry_after is not None
            else "retry shortly."
        )
        return (
            "API is rate-limited (HTTP 429). Token was saved anyway — " + wait
        )
    except TransientError as error:
        return f"Could not validate token ({error}). Saved anyway."
    return None


def _persist_new_account(
    store: AccountStore,
    account: Account,
    provider: Provider,
    source_codex_home: Path | None,
    *,
    reference_time: datetime,
) -> None:
    """Persist authority before creating a new private Codex bundle."""
    if provider.id is not ProviderId.CODEX:
        store.persist(account)
        return
    store.persist(account)
    if _write_sidekick_codex_cache(
        account,
        provider,
        source_codex_home,
        _get_ctx().private_codex_locations,
        reference_time=reference_time,
    ):
        store.persist(account)


def _insert_new(
    provider: Provider,
    detected: DetectedCredentials,
    label_override: str | None,
    plan: str | None,
    force: bool,
    *,
    source_codex_home: Path | None,
    reference_time: datetime,
) -> None:
    """Fresh-token path: not yet stored.

    :param provider: Provider this token belongs to.
    :param detected: Normalized provider credentials.
    :param label_override: User-supplied label, if any.
    :param plan: Plan tag, if any.
    :param force: Overwrite an existing target label.
    :param reference_time: Aware time shared by credential file writes.
    """
    app_ctx = _get_ctx()
    store = app_ctx.require_store()
    label = (
        _validated_label(label_override)
        if label_override is not None
        else store.generate_label(provider.id, plan or "account")
    )
    if label in store and not force:
        app_ctx.err_console.print(
            f"[yellow]Account '{label}' already exists. Use "
            f"--force or pass --label.[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)

    acct = Account(
        label=label,
        credentials=detected.credentials,
        plan=plan or "unknown",
    )
    warning = _new_account_warning(acct, provider)
    _persist_new_account(
        store,
        acct,
        provider,
        source_codex_home,
        reference_time=reference_time,
    )

    app_ctx.console.print(f"[green]Saved '{label}'.[/green]")
    if warning:
        app_ctx.console.print(f"[yellow]Note: {warning}[/yellow]")
    app_ctx.console.print(
        f"[dim]Rename any time with: sidekick-usages rename "
        f"{label} <new-name>[/dim]"
    )


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


def _print_forbidden(provider: Provider, err: ForbiddenError) -> None:
    """Render an unexpected 403 from the usage endpoint at add-time.

    Reached only when the 403 doesn't fit the canonical
    inference-only self-heal case — i.e. a different missing
    scope, or the response carried no parseable scope name. The
    token is still saved by the caller; this just surfaces what
    the API said so the user can investigate.

    :param provider: Provider the token was being added for.
    :param err: The parsed forbidden error carrying API body and
        required-scope details.
    """
    app_ctx = _get_ctx()
    detail = (
        f"required scope {err.required_scope!r}"
        if err.required_scope
        else "no scope name returned"
    )
    app_ctx.console.print(
        f"[yellow]Note: {provider.display_name} usage endpoint "
        f"returned HTTP 403 ({detail}).[/yellow]"
    )
    if err.api_message:
        app_ctx.console.print(f"[yellow]API: {err.api_message}[/yellow]")


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

"""Command-line entry point.

Typer-based CLI. Each subcommand is a top-level function decorated
with ``@app.command()``. State lives in a lazily initialized
:class:`AppContext`; tests inject fakes through :func:`set_context`.
"""

import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Annotated, NoReturn

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
    ExpiredExpiry,
    InvalidExpiry,
    UnknownExpiry,
    ValidExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
    DetectedCredentials,
    UsageReport,
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
    doctor_exit_code,
    render_doctor,
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
    claude_lifetime_output,
    codex_lifetime_output,
)
from sidekick_usages.maintenance import (
    RefreshOutcome,
    TokenMaintenanceService,
    record_refresh_failure,
    record_refresh_success,
    refresh_exit_code,
)
from sidekick_usages.paths import (
    PrivateCodexLocations,
    discover_application_paths,
)
from sidekick_usages.providers import build_provider_registry
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.codex import (
    CodexProvider,
    auth_blob_matches_account,
    default_codex_home,
    ensure_file_auth_home,
    read_auth_blob,
    write_account_auth_file,
)
from sidekick_usages.render import FetchFailure, usage_overview
from sidekick_usages.serialization import JsonObject
from sidekick_usages.store import AccountStore
from sidekick_usages.token_input import TokenInput
from sidekick_usages.update import (
    InstallMethod,
    detect_install_method,
    fetch_latest_release,
    is_newer,
    manual_instructions,
    upgrade_command_for,
)


# ---------------------------------------------------------------------
# App context: injectable state
# ---------------------------------------------------------------------
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
    :ivar only: Provider filter applied to ``check`` (``--only``).
    """

    store: AccountStore
    http: HttpClient
    providers: dict[ProviderId, Provider]
    heartbeat_providers: dict[ProviderId, HeartbeatProvider]
    private_codex_locations: PrivateCodexLocations
    lifetime_sources: dict[ProviderId, Callable[[], tuple[int, str | None]]]
    console: Console
    err_console: Console
    clock: Clock
    only: ProviderId | None = None
    collected: list[tuple[Account, UsageReport]] = field(default_factory=list)
    failures: list[tuple[Account, FetchFailure]] = field(default_factory=list)


_SAFE_CODEX_CACHE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_MIN_TOKEN_LENGTH_FOR_MASKING = 30
_DAEMON_BACKEND_HELP = (
    "Scheduler backend: auto, systemd, cron, launchd, task-scheduler."
)


def _build_default_context() -> AppContext:
    """Construct the default production app context.

    :return: An :class:`AppContext` wired with real dependencies.
    """
    paths = discover_application_paths()
    clock = SystemClock()
    providers = build_provider_registry(clock)
    with ExitStack() as cleanup:
        http = cleanup.enter_context(HttpClient(clock=clock))
        context = AppContext(
            store=AccountStore(paths.accounts).load(),
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


def _get_ctx() -> AppContext:
    """Return the active app context, building one if needed.

    :return: The active :class:`AppContext`.
    """
    if _ContextState.ctx is None:
        _ContextState.ctx = _build_default_context()
    return _ContextState.ctx


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
    app_ctx = _get_ctx()
    ctx.call_on_close(app_ctx.http.close)
    try:
        provider_filter = ProviderId(only) if only is not None else None
    except ValueError:
        app_ctx.err_console.print(
            f"[red]Unknown provider {only!r}. "
            f"Known: {', '.join(sorted(app_ctx.providers))}.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None
    app_ctx.only = provider_filter
    if ctx.invoked_subcommand is None:
        _do_check()


# ---------------------------------------------------------------------
# check (default)
# ---------------------------------------------------------------------
@app.command("check")
def check_cmd() -> None:
    """Print usage for every saved account."""
    _do_check()


def _do_check() -> None:
    """Fetch all (filtered) accounts and render the grouped overview.

    Exits with code 1 if any account failed.
    """
    app_ctx = _get_ctx()
    app_ctx.collected.clear()
    app_ctx.failures.clear()
    accounts = list(app_ctx.store)
    if app_ctx.only:
        accounts = [a for a in accounts if a.provider_id == app_ctx.only]
    if not accounts:
        _print_no_accounts(app_ctx.only, branded=True)
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)

    exit_code = ExitCode.SUCCESS
    for acct in accounts:
        if not _fetch_and_render(acct):
            exit_code = ExitCode.MANUAL_ACTION

    if app_ctx.collected or app_ctx.failures:
        app_ctx.console.print(
            usage_overview(
                app_ctx.collected,
                _lifetime_for(
                    app_ctx.collected,
                    app_ctx.lifetime_sources,
                ),
                failures=app_ctx.failures,
                width=app_ctx.console.size.width,
                reference_time=app_ctx.clock.now(),
            )
        )
    if exit_code:
        raise typer.Exit(code=exit_code)


def _collect(acct: Account, report: UsageReport) -> None:
    """Stash a successful report for the end-of-run grouped render."""
    _get_ctx().collected.append((acct, report))


def _lifetime_for(
    pairs: list[tuple[Account, UsageReport]],
    sources: dict[ProviderId, Callable[[], tuple[int, str | None]]],
) -> dict[ProviderId, tuple[int, str | None]]:
    """Look up lifetime output per provider present in ``pairs``."""
    providers = {acct.provider_id for acct, _ in pairs}
    return {
        provider_id: source()
        for provider_id, source in sources.items()
        if provider_id in providers
    }


#: Scope required to read the OAuth usage endpoint. Matches the
#: ``gLH`` constant in the Claude Code binary; the in-tree ``hT()``
#: predicate gates ``/api/oauth/usage`` on whether the stored
#: credentials' ``scopes`` array contains exactly this string.
_USAGE_REQUIRED_SCOPE = "user:profile"


def _handle_runtime_forbidden(
    acct: Account,
    provider: Provider,
    err: ForbiddenError,
) -> bool:
    """Handle a 403 raised during ``check`` for an unknown-scope acct.

    The OAuth usage endpoint refused this token. If the 403 is the
    canonical "needs ``user:profile``" case and we have no scope
    info on file, self-heal ``scopes=[]`` so the provider routes to
    the header probe (which works for inference-only tokens), then
    retry the fetch. Any other 403 (different scope, different
    endpoint shape) is surfaced as a per-account error block.

    :param acct: Account whose request 403'd.
    :param provider: Provider for ``acct``.
    :param err: Parsed forbidden error.
    :return: True when the retry rendered real usage, False when
        rendered as an error.
    """
    app_ctx = _get_ctx()
    if (
        acct.scopes is None
        and err.required_scope == _USAGE_REQUIRED_SCOPE
        and provider.id is ProviderId.CLAUDE
    ):
        credentials = _claude_credentials(acct)
        acct.credentials = replace(credentials, scopes=())
        app_ctx.store.upsert(acct)
        app_ctx.store.save()
        try:
            report = provider.fetch_usage(acct, app_ctx.http)
        except UsageError as retry_err:
            _record_error_block(acct, f"Header probe failed: {retry_err}")
            return False
        _collect(acct, report)
        return True
    detail = err.api_message or str(err)
    msg = f"Forbidden (HTTP 403): {detail}"
    if err.required_scope:
        msg += f"\n  Required scope: {err.required_scope}."
    _record_error_block(acct, msg)
    return False


def _fetch_and_render(acct: Account) -> bool:
    """Fetch one account's usage; on 401, try refresh once.

    :param acct: Account to query.
    :return: True on success, False on any error.
    """
    app_ctx = _get_ctx()
    provider = app_ctx.providers.get(acct.provider_id)
    if provider is None:
        _record_error_block(
            acct,
            f"Unknown provider '{acct.provider_id}'.",
        )
        return False
    if not _refresh_known_expired(acct, provider):
        return False
    try:
        return _fetch_usage_and_render(acct, provider)
    except AuthError as e:
        return _refresh_after_auth_and_render(acct, provider, e)
    except ForbiddenError as e:
        return _handle_fetch_error(acct, provider, e)
    except UsageError as e:
        return _handle_fetch_error(acct, provider, e)


def _fetch_usage_and_render(acct: Account, provider: Provider) -> bool:
    """Fetch and render usage for one account.

    :param acct: Account to query.
    :param provider: Provider for ``acct``.
    :return: True after rendering usage.
    """
    app_ctx = _get_ctx()
    before_credentials = acct.credentials
    before_plan = acct.plan
    report = provider.fetch_usage(acct, app_ctx.http)
    if report.plan and report.plan not in ("unknown", acct.plan):
        acct.plan = report.plan
    if acct.credentials != before_credentials or acct.plan != before_plan:
        app_ctx.store.upsert(acct)
        app_ctx.store.save()
    _collect(acct, report)
    return True


def _refresh_known_expired(acct: Account, provider: Provider) -> bool:
    """Refresh a known-expired account before its first fetch.

    :param acct: Account about to be queried.
    :param provider: Provider for ``acct``.
    :return: False only when refresh itself errors.
    """
    if isinstance(acct.expiry, UnknownExpiry):
        return True
    if isinstance(acct.expiry, InvalidExpiry):
        _record_error_block(
            acct,
            "Access-token expiry metadata is invalid; refresh the account.",
        )
        return False
    reference_time = _get_ctx().clock.now()
    if not _should_refresh_before_fetch(acct, provider, reference_time):
        return True
    try:
        refreshed = _refresh_and_save(acct, provider)
    except UsageError as e:
        _record_error_block(acct, f"Token refresh failed: {e}")
        return False
    if not refreshed:
        _record_auth_failure(acct)
        return False
    return True


def _refresh_after_auth_and_render(
    acct: Account,
    provider: Provider,
    err: AuthError,
) -> bool:
    """Refresh after a 401, then retry usage once.

    :param acct: Account whose fetch returned 401.
    :param provider: Provider for ``acct``.
    :param err: Original auth error to render if refresh cannot help.
    :return: True on successful retry, otherwise False.
    """
    try:
        refreshed = _refresh_and_save(acct, provider)
    except UsageError as refresh_err:
        _record_error_block(acct, f"Token refresh failed: {refresh_err}")
        return False
    if not refreshed:
        return _handle_fetch_error(acct, provider, err)
    try:
        return _fetch_usage_and_render(acct, provider)
    except UsageError as retry_err:
        return _handle_fetch_error(acct, provider, retry_err)


def _should_refresh_before_fetch(
    acct: Account,
    provider: Provider,
    reference_time: datetime,
) -> bool:
    """Return whether a known-expired account should refresh first.

    :param acct: Account about to be queried.
    :param provider: Provider for ``acct``.
    :param reference_time: Aware wall time for the expiry decision.
    :return: True when a provider-specific expiry is already stale.
    """
    expiry = classify_expiry(acct.expiry, now=reference_time)
    if isinstance(expiry, ExpiredExpiry):
        return True
    if isinstance(expiry, ValidExpiry):
        if provider.id is ProviderId.CODEX:
            return expiry.at <= reference_time + timedelta(seconds=60)
        return False
    return False


def _refresh_and_save(acct: Account, provider: Provider) -> bool:
    """Refresh an account token and persist any successful mutation.

    :param acct: Account to refresh.
    :param provider: Provider for ``acct``.
    :return: True when refresh succeeded.
    """
    app_ctx = _get_ctx()
    try:
        refreshed = provider.refresh_token(acct, app_ctx.http)
    except UsageError as e:
        record_refresh_failure(acct, str(e), app_ctx.clock.now())
        app_ctx.store.upsert(acct)
        app_ctx.store.save()
        raise
    if not refreshed:
        record_refresh_failure(
            acct,
            "Refresh token unavailable or rejected.",
            app_ctx.clock.now(),
        )
        app_ctx.store.upsert(acct)
        app_ctx.store.save()
        return False
    record_refresh_success(acct, app_ctx.clock.now())
    app_ctx.store.upsert(acct)
    app_ctx.store.save()
    return True


def _handle_fetch_error(
    acct: Account,
    provider: Provider,
    err: UsageError,
) -> bool:
    """Render one fetch failure as a per-account result.

    :param acct: Account whose fetch failed.
    :param provider: Provider for ``acct``.
    :param err: Typed usage error to render.
    :return: Always False unless a forbidden self-heal succeeds.
    """
    if isinstance(err, AuthError):
        _record_auth_failure(acct)
        return False
    if isinstance(err, ForbiddenError):
        return _handle_runtime_forbidden(acct, provider, err)
    if isinstance(err, RateLimitError):
        return _handle_rate_limit(acct, err)
    if isinstance(err, TransientError):
        return _handle_transient(acct, err)
    _record_error_block(acct, str(err))
    return False


def _handle_rate_limit(acct: Account, err: RateLimitError) -> bool:
    """Render a per-account rate-limit error.

    :param acct: Account whose request was rate-limited.
    :param err: Rate-limit error with optional retry delay.
    :return: False.
    """
    suffix = (
        f"Server asked to wait {err.retry_after}s."
        if err.retry_after is not None
        else "Try again in a moment."
    )
    _record_error_block(
        acct,
        f"Rate limited (HTTP 429). {suffix}",
    )
    return False


def _handle_transient(acct: Account, err: TransientError) -> bool:
    """Render a per-account transient error.

    :param acct: Account whose request failed transiently.
    :param err: Transient error to display.
    :return: False.
    """
    _record_error_block(acct, str(err))
    return False


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
    existing = app_ctx.store.find_by_token(prov.id, token)
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
    accounts = list(app_ctx.store)
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
    app_ctx.console.print(f"\n[dim]Config: {app_ctx.store.path}[/dim]")


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
    if not app_ctx.store.remove(label):
        app_ctx.err_console.print(
            f"[yellow]No account named '{label}'.[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    app_ctx.store.save()
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
    if not app_ctx.store.rename(old, new):
        app_ctx.err_console.print(
            f"[yellow]Cannot rename: '{old}' is missing or "
            f"'{new}' already exists.[/yellow]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    app_ctx.store.save()
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
    acct = app_ctx.store.get(label)
    if acct is None:
        app_ctx.err_console.print(f"[red]No account labeled '{label}'.[/red]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    acct.plan = value
    app_ctx.store.upsert(acct)
    app_ctx.store.save()
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
    acct = app_ctx.store.get(label)
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
    app_ctx.store.upsert(acct)
    app_ctx.store.save()
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


def _refresh_all_cmd(*, quiet: bool, force: bool) -> None:
    """Run scheduler-safe saved-token refresh for all accounts."""
    app_ctx = _get_ctx()
    accounts = list(app_ctx.store)
    if not accounts:
        _print_no_accounts(None)
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    service = TokenMaintenanceService(
        app_ctx.store,
        app_ctx.http,
        app_ctx.providers,
        clock=app_ctx.clock,
    )
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
    account = app_ctx.store.get(label)
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
        app_ctx.store.get(label),
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
        app_ctx.store.get(label),
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
    accounts = list(app_ctx.store)
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
    accounts = list(app_ctx.store)
    if not accounts:
        _print_no_accounts(None)
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)

    refresh_service = TokenMaintenanceService(
        app_ctx.store,
        app_ctx.http,
        app_ctx.providers,
        clock=app_ctx.clock,
    )
    refresh_outcomes = refresh_service.refresh_all()
    _render_refresh_outcomes(refresh_outcomes, quiet=quiet)

    heartbeat_outcomes = HeartbeatService(
        app_ctx.store,
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
        app_ctx.store,
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
# doctor
# ---------------------------------------------------------------------
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
    service = DoctorService(
        app_ctx.store,
        app_ctx.providers,
        app_ctx.heartbeat_providers,
        TokenMaintenanceService(
            app_ctx.store,
            app_ctx.http,
            app_ctx.providers,
            clock=app_ctx.clock,
        ),
        clock=app_ctx.clock,
    )
    diagnostics = service.diagnostics(provider_id=provider_filter, label=label)
    if not diagnostics:
        app_ctx.err_console.print("[yellow]No matching accounts.[/yellow]")
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    render_doctor(diagnostics, app_ctx.console, json_output=json_output)
    code = doctor_exit_code(diagnostics)
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
    if source_home is not None:
        ensure_file_auth_home(source_home)

    argv = ["codex", "login"]
    if device_auth:
        argv.append("--device-auth")
    try:
        if source_home is None:
            subprocess.run(argv, check=True)
        else:
            env = os.environ.copy()
            env["CODEX_HOME"] = str(source_home)
            subprocess.run(argv, check=True, env=env)
    except FileNotFoundError as e:
        app_ctx.err_console.print(
            "[red]Codex CLI executable 'codex' was not found on PATH.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from e
    except subprocess.CalledProcessError as e:
        raise typer.Exit(code=e.returncode) from e

    detected = provider.detect_credentials(source_home)
    if detected is None:
        source = source_home or default_codex_home()
        app_ctx.err_console.print(
            f"[red]Codex login finished, but no auth.json was found in "
            f"{source}.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)

    acct = app_ctx.store.get(label)
    if acct is not None and acct.provider_id is not ProviderId.CODEX:
        app_ctx.err_console.print(
            f"[red]'{label}' is a {acct.provider_id} account, not codex.[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION)
    if acct is None:
        if not isinstance(detected.credentials, CodexCredentials):
            app_ctx.err_console.print(
                "[red]Codex returned incompatible credentials.[/red]"
            )
            raise typer.Exit(code=ExitCode.SYSTEM_ERROR)
        acct = Account(
            label=AccountLabel(label),
            credentials=detected.credentials,
        )
    else:
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
        source_home,
        app_ctx.private_codex_locations,
        reference_time=reference_time,
    )
    app_ctx.store.upsert(acct)
    app_ctx.store.save()
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
    acct = app_ctx.store.get(label)
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
        _refresh_and_save(acct, provider)

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

    app_ctx.store.upsert(acct)
    app_ctx.store.save()
    app_ctx.console.print(
        f"[green]Exported '{label}' to Codex home {codex_home}.[/green]"
    )


# ---------------------------------------------------------------------
# setup-token
# ---------------------------------------------------------------------
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
        token = prov.run_setup_token()
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
    existing = app_ctx.store.find_by_token(prov.id, token)
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
    if provider:
        try:
            provider_id = ProviderId(provider)
        except ValueError:
            app_ctx.err_console.print(
                f"[red]Unknown provider {provider!r}.[/red]"
            )
            raise typer.Exit(code=ExitCode.MANUAL_ACTION) from None
        targets = app_ctx.store.filter_by_provider(provider_id)
        count = len(targets)
        scope = f"{count} {provider} account(s)"
    else:
        count = len(app_ctx.store)
        scope = f"{count} saved account(s) and remove {app_ctx.store.path}"
    if count == 0:
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
        cleared = app_ctx.store.reset_provider(provider_id)
        app_ctx.console.print(
            f"[green]Cleared {cleared} {provider} account(s).[/green]"
        )
    else:
        cleared = app_ctx.store.reset()
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
    return locations.canonical / safe


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
    return write_account_auth_file(
        acct,
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
    target = (
        _validated_label(label_override)
        if label_override is not None
        else existing.label
    )
    if target != existing.label:
        if target in app_ctx.store and not force:
            app_ctx.err_console.print(
                f"[yellow]Token already saved as "
                f"'{existing.label}', but '{target}' already "
                f"exists too. Use --force to overwrite.[/yellow]"
            )
            raise typer.Exit(code=ExitCode.MANUAL_ACTION)
        app_ctx.store.rename(existing.label, target)
    acct = app_ctx.store.get(target)
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
        app_ctx.store.upsert(acct)
    app_ctx.store.save()
    app_ctx.console.print(
        f"[green]Token already saved as '{target}' — updated in place.[/green]"
    )


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
    label = (
        _validated_label(label_override)
        if label_override is not None
        else app_ctx.store.generate_label(provider.id, plan or "account")
    )
    if label in app_ctx.store and not force:
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

    warning: str | None = None
    try:
        provider.fetch_usage(acct, app_ctx.http)
    except AuthError as e:
        app_ctx.err_console.print(
            "[red]Token rejected by API (HTTP 401).[/red]"
        )
        raise typer.Exit(code=ExitCode.MANUAL_ACTION) from e
    except ForbiddenError as e:
        # OAuth usage endpoint refused — likely an inference-only
        # token (e.g. ``claude setup-token``). Self-heal scopes=[]
        # so fetch_usage routes to the header probe, then retry to
        # validate that path works too. The probe also primes the
        # in-memory ``acct`` so a follow-up ``check`` returns
        # usage immediately without re-paying the discovery 403.
        if e.required_scope == _USAGE_REQUIRED_SCOPE and acct.scopes is None:
            credentials = _claude_credentials(acct)
            acct.credentials = replace(credentials, scopes=())
            try:
                provider.fetch_usage(acct, app_ctx.http)
            except UsageError as retry_err:
                warning = (
                    f"Token saved, but the header probe also "
                    f"failed: {retry_err}"
                )
        else:
            _print_forbidden(provider, e)
    except RateLimitError as e:
        wait = (
            f"retry in {e.retry_after}s."
            if e.retry_after is not None
            else "retry shortly."
        )
        warning = (
            f"API is rate-limited (HTTP 429). Token was saved anyway — {wait}"
        )
    except TransientError as e:
        warning = f"Could not validate token ({e}). Saved anyway."

    app_ctx.store.upsert(acct)
    _write_sidekick_codex_cache(
        acct,
        provider,
        source_codex_home,
        app_ctx.private_codex_locations,
        reference_time=reference_time,
    )
    app_ctx.store.save()

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


def _record_error_block(acct: Account, message: str) -> None:
    """Record a generic per-account fetch failure for in-panel render."""
    app_ctx = _get_ctx()
    detail = tuple(message.splitlines())
    app_ctx.failures.append(
        (acct, FetchFailure(status="error", detail=detail))
    )


def _record_auth_failure(acct: Account) -> None:
    """Record a 401 as an in-panel failure with a re-login + refresh hint."""
    app_ctx = _get_ctx()
    provider = app_ctx.providers.get(acct.provider_id)
    display = provider.display_name if provider else acct.provider_id
    app_ctx.failures.append(
        (
            acct,
            FetchFailure(
                status="token expired",
                detail=(
                    f"Log in to {display} again, then run:",
                    f"sidekick-usages refresh {shlex.quote(acct.label)}",
                ),
            ),
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
def _run_typer() -> int:
    """Invoke Typer and convert :class:`UsageError` to exit-1.

    :return: Process exit code.
    """
    try:
        app(standalone_mode=False)
        return 0
    except typer.Exit as e:
        return int(e.exit_code or 0)
    except UsageError as e:
        Console(stderr=True).print(f"[red]{e}[/red]")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted.\n")
        return 130


if __name__ == "__main__":
    sys.exit(_run_typer())

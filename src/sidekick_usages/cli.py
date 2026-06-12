"""Command-line entry point.

Typer-based CLI. Each subcommand is a top-level function decorated
with ``@app.command()``. State lives in a module-level
:class:`AppContext` that command functions read from, so tests can
inject fakes by overwriting ``_ctx``.
"""

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from sidekick_usages import __version__
from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    RateLimitError,
    TransientError,
    UnsupportedOperationError,
    UsageError,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.providers import PROVIDERS
from sidekick_usages.providers.base import DetectedCredentials, Provider
from sidekick_usages.providers.codex import (
    CodexProvider,
    auth_blob_matches_account,
    default_codex_home,
    ensure_file_auth_home,
    read_auth_blob,
    write_account_auth_file,
)
from sidekick_usages.render import account_header, usage_report
from sidekick_usages.store import CONFIG_DIR, Account, AccountStore
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
    :ivar console: Rich console for stdout.
    :ivar err_console: Rich console pinned to stderr.
    :ivar only: Provider filter applied to ``check`` (``--only``).
    """

    store: AccountStore
    http: HttpClient
    providers: dict[str, Provider]
    console: Console
    err_console: Console
    only: str | None = None


@dataclass
class _CredentialFields:
    """Normalized credential metadata ready to save."""

    token: str
    refresh_token: str | None = None
    expires_at: int | None = None
    scopes: list[str] | None = None
    provider_account_id: str | None = None
    source_codex_home: Path | None = None
    codex_id_token: str | None = None
    codex_last_refresh: str | None = None


CODEX_CACHE_DIR = CONFIG_DIR / "codex"
_SAFE_CODEX_CACHE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _build_default_context() -> AppContext:
    """Construct the default production app context.

    :return: An :class:`AppContext` wired with real dependencies.
    """
    return AppContext(
        store=AccountStore().load(),
        http=HttpClient(),
        providers=PROVIDERS,
        console=Console(),
        err_console=Console(stderr=True),
    )


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
app = typer.Typer(
    name="sidekick-usages",
    help=(
        "Check Claude Code and Codex CLI usage across multiple "
        "accounts in one command."
    ),
    rich_markup_mode="rich",
    no_args_is_help=False,
    pretty_exceptions_show_locals=False,
    add_completion=False,
)


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
    if only is not None and only not in app_ctx.providers:
        app_ctx.err_console.print(
            f"[red]Unknown provider {only!r}. "
            f"Known: {', '.join(sorted(app_ctx.providers))}.[/red]"
        )
        raise typer.Exit(code=1)
    app_ctx.only = only
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
    """Render all (filtered) accounts.

    Exits with code 1 if any account failed.
    """
    app_ctx = _get_ctx()
    accounts = list(app_ctx.store)
    if app_ctx.only:
        accounts = [a for a in accounts if a.provider_id == app_ctx.only]
    if not accounts:
        _print_no_accounts(app_ctx.only)
        raise typer.Exit(code=1)

    exit_code = 0
    for i, acct in enumerate(accounts):
        if i:
            app_ctx.console.print()
        ok = _fetch_and_render(acct)
        if not ok:
            exit_code = 1
    if exit_code:
        raise typer.Exit(code=exit_code)


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
        and provider.id == "claude"
    ):
        acct.scopes = []
        app_ctx.store.upsert(acct)
        app_ctx.store.save()
        try:
            report = provider.fetch_usage(acct, app_ctx.http)
        except UsageError as retry_err:
            _print_error_block(acct, f"Header probe failed: {retry_err}")
            return False
        app_ctx.console.print(usage_report(acct, report))
        return True
    detail = err.api_message or str(err)
    msg = f"Forbidden (HTTP 403): {detail}"
    if err.required_scope:
        msg += f"\n  Required scope: {err.required_scope}."
    _print_error_block(acct, msg)
    return False


def _fetch_and_render(acct: Account) -> bool:
    """Fetch one account's usage; on 401, try refresh once.

    :param acct: Account to query.
    :return: True on success, False on any error.
    """
    app_ctx = _get_ctx()
    provider = app_ctx.providers.get(acct.provider_id)
    if provider is None:
        _print_error_block(
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
        return _retry_or_handle_forbidden(acct, provider, e)
    except UsageError as e:
        return _handle_fetch_error(acct, provider, e)


def _fetch_usage_and_render(acct: Account, provider: Provider) -> bool:
    """Fetch and render usage for one account.

    :param acct: Account to query.
    :param provider: Provider for ``acct``.
    :return: True after rendering usage.
    """
    app_ctx = _get_ctx()
    before_fetch = acct.to_dict()
    report = provider.fetch_usage(acct, app_ctx.http)
    if report.plan and report.plan not in ("unknown", acct.plan):
        acct.plan = report.plan
    if acct.to_dict() != before_fetch:
        app_ctx.store.upsert(acct)
        app_ctx.store.save()
    app_ctx.console.print(usage_report(acct, report))
    return True


def _refresh_known_expired(acct: Account, provider: Provider) -> bool:
    """Refresh a known-expired account before its first fetch.

    :param acct: Account about to be queried.
    :param provider: Provider for ``acct``.
    :return: False only when refresh itself errors.
    """
    if not _should_refresh_before_fetch(acct, provider):
        return True
    try:
        _refresh_and_save(acct, provider)
    except UsageError as e:
        _print_error_block(acct, f"Token refresh failed: {e}")
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
        _print_error_block(acct, f"Token refresh failed: {refresh_err}")
        return False
    if not refreshed:
        return _handle_fetch_error(acct, provider, err)
    try:
        return _fetch_usage_and_render(acct, provider)
    except UsageError as retry_err:
        return _handle_fetch_error(acct, provider, retry_err)


def _should_refresh_before_fetch(acct: Account, provider: Provider) -> bool:
    """Return whether a known-expired account should refresh first.

    :param acct: Account about to be queried.
    :param provider: Provider for ``acct``.
    :return: True when a provider-specific expiry is already stale.
    """
    if acct.expires_at is None:
        return False
    if provider.id == "claude":
        return acct.expires_at <= int(time.time() * 1000)
    if provider.id == "codex":
        return acct.expires_at <= int(time.time()) + 60
    return False


def _refresh_and_save(acct: Account, provider: Provider) -> bool:
    """Refresh an account token and persist any successful mutation.

    :param acct: Account to refresh.
    :param provider: Provider for ``acct``.
    :return: True when refresh succeeded.
    """
    app_ctx = _get_ctx()
    if not provider.refresh_token(acct, app_ctx.http):
        return False
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
        _print_auth_error_block(acct)
        return False
    if isinstance(err, ForbiddenError):
        return _handle_runtime_forbidden(acct, provider, err)
    if isinstance(err, RateLimitError):
        return _handle_rate_limit(acct, err)
    if isinstance(err, TransientError):
        return _handle_transient(acct, err)
    _print_error_block(acct, str(err))
    return False


def _retry_or_handle_forbidden(
    acct: Account,
    provider: Provider,
    err: ForbiddenError,
) -> bool:
    """Retry transient Codex 403s, otherwise render the error."""
    if not _should_retry_bodyless_forbidden(acct, provider, err):
        return _handle_fetch_error(acct, provider, err)
    try:
        return _fetch_usage_and_render(acct, provider)
    except UsageError as retry_err:
        return _handle_fetch_error(acct, provider, retry_err)


def _should_retry_bodyless_forbidden(
    acct: Account,
    provider: Provider,
    err: ForbiddenError,
) -> bool:
    """Return whether a bodyless 403 should be retried once."""
    del acct
    return (
        provider.id == "codex"
        and err.api_message is None
        and err.required_scope is None
    )


def _handle_rate_limit(acct: Account, err: RateLimitError) -> bool:
    """Render a per-account rate-limit error.

    :param acct: Account whose request was rate-limited.
    :param err: Rate-limit error with optional retry delay.
    :return: False.
    """
    suffix = (
        f"Server asked to wait {err.retry_after}s."
        if err.retry_after
        else "Try again in a moment."
    )
    _print_error_block(
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
    _print_error_block(acct, str(err))
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

    refresh: str | None = None
    expires_at: int | None = None
    scopes: list[str] | None = None
    provider_account_id: str | None = None
    codex_id_token: str | None = None
    codex_last_refresh: str | None = None
    normalized_codex_home = _normalize_codex_home(prov, codex_home)

    if not token:
        detected = prov.detect_credentials(normalized_codex_home)
        if detected:
            token = detected.access_token
            provider_account_id = detected.provider_account_id
            refresh = detected.refresh_token
            expires_at = detected.expires_at
            scopes = detected.scopes
            codex_id_token = detected.id_token
            codex_last_refresh = detected.last_refresh
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
                raise typer.Exit(code=1)
            src = "stdin" if not sys.stdin.isatty() else "prompt"
            app_ctx.console.print(f"[green]Got token from {src}.[/green]")

    existing = app_ctx.store.find_by_token(token)
    fields = _CredentialFields(
        token=token,
        refresh_token=refresh,
        expires_at=expires_at,
        scopes=scopes,
        provider_account_id=provider_account_id,
        source_codex_home=normalized_codex_home,
        codex_id_token=codex_id_token,
        codex_last_refresh=codex_last_refresh,
    )
    if existing is not None:
        _upsert_existing(
            existing,
            label,
            plan,
            force,
            fields=fields,
        )
        return
    _insert_new(
        prov,
        fields,
        label,
        plan,
        force,
    )


# ---------------------------------------------------------------------
# list
# ---------------------------------------------------------------------
@app.command("list")
def list_cmd() -> None:
    """List every saved account."""
    app_ctx = _get_ctx()
    accounts = list(app_ctx.store)
    if not accounts:
        app_ctx.console.print("[dim](no accounts saved)[/dim]")
        return

    table = Table(
        title="[bold]Saved accounts[/bold]",
        title_justify="left",
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 2),
        pad_edge=False,
    )
    table.add_column("Label", no_wrap=True)
    table.add_column("Provider", no_wrap=True)
    table.add_column("Plan", no_wrap=True)
    table.add_column("Token", no_wrap=True, style="dim")

    for acct in accounts:
        prov_color = "magenta" if acct.provider_id == "claude" else "cyan"
        plan_text = (
            Text(acct.plan, style="dim")
            if acct.plan == "unknown"
            else Text(acct.plan)
        )
        table.add_row(
            acct.label,
            Text(acct.provider_id, style=prov_color),
            plan_text,
            acct.masked_token(),
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
        raise typer.Exit(code=1)
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
        raise typer.Exit(code=1)
    app_ctx.store.save()
    app_ctx.console.print(f"[green]Renamed '{old}' → '{new}'.[/green]")


# ---------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------
@app.command("refresh")
def refresh_cmd(
    label: Annotated[str, typer.Argument(help="Account label.")],
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

    Reads the current login from the provider's local install
    (macOS Keychain, Linux files, or Windows Credential Manager)
    and writes the new access token into the saved account. When the
    provider exposes stable account ids, the detected login must match
    the saved account unless ``--replace-identity`` is passed.
    """
    app_ctx = _get_ctx()
    acct = app_ctx.store.get(label)
    if acct is None:
        app_ctx.err_console.print(
            f"[yellow]No account named '{label}'.[/yellow]"
        )
        raise typer.Exit(code=1)
    provider = app_ctx.providers.get(acct.provider_id)
    if provider is None:
        app_ctx.err_console.print(
            f"[red]Unknown provider '{acct.provider_id}' for '{label}'.[/red]"
        )
        raise typer.Exit(code=1)
    credential_home = _refresh_credential_home(
        acct,
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
        raise typer.Exit(code=1)
    _ensure_refresh_identity_matches(
        acct,
        detected,
        label,
        replace_identity=replace_identity,
    )
    _apply_detected_credentials(acct, detected, provider, credential_home)
    app_ctx.store.upsert(acct)
    app_ctx.store.save()
    app_ctx.console.print(f"[green]Updated token for '{label}'.[/green]")


def _ensure_refresh_identity_matches(
    acct: Account,
    detected: DetectedCredentials,
    label: str,
    *,
    replace_identity: bool,
) -> None:
    """Reject manual refresh when the active local login is a different account.

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
    raise typer.Exit(code=1)


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
        raise typer.Exit(code=1) from e
    except subprocess.CalledProcessError as e:
        raise typer.Exit(code=e.returncode) from e

    detected = provider.detect_credentials(source_home)
    if detected is None:
        source = source_home or default_codex_home()
        app_ctx.err_console.print(
            f"[red]Codex login finished, but no auth.json was found in "
            f"{source}.[/red]"
        )
        raise typer.Exit(code=1)

    acct = app_ctx.store.get(label)
    if acct is not None and acct.provider_id != "codex":
        app_ctx.err_console.print(
            f"[red]'{label}' is a {acct.provider_id} account, not codex.[/red]"
        )
        raise typer.Exit(code=1)
    if acct is None:
        acct = Account(
            label=label,
            provider_id="codex",
            access_token=detected.access_token,
        )
    else:
        _ensure_refresh_identity_matches(
            acct,
            detected,
            label,
            replace_identity=replace_identity,
        )
    _apply_detected_credentials(acct, detected, provider, source_home)
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
        raise typer.Exit(code=1)
    if acct.provider_id != "codex":
        app_ctx.err_console.print(
            f"[red]'{label}' is a {acct.provider_id} account, not codex.[/red]"
        )
        raise typer.Exit(code=1)

    source_blob = _matching_codex_auth_blob(
        acct,
        source_codex_home,
        Path(acct.codex_home).expanduser() if acct.codex_home else None,
        default_codex_home(),
    )
    if source_blob is not None:
        _apply_matching_codex_blob(acct, source_blob, provider)
    elif not acct.codex_id_token and acct.refresh_token:
        _refresh_and_save(acct, provider)

    if not write_account_auth_file(
        acct,
        codex_home.expanduser(),
        source_blob=source_blob,
    ):
        app_ctx.err_console.print(
            "[red]Cannot export a complete Codex auth file for "
            f"'{label}'.[/red]\n"
            "  Missing Codex id_token or account id metadata. Run "
            f"`sidekick-usages codex-login {label}` once for that account."
        )
        raise typer.Exit(code=1)

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
        raise typer.Exit(code=1) from e

    if not token:
        app_ctx.err_console.print(
            f"[red]Did not capture a token. Try again or run "
            f"`sidekick-usages add {prov.id}` with --token."
            f"[/red]"
        )
        raise typer.Exit(code=1)

    existing = app_ctx.store.find_by_token(token)
    fields = _CredentialFields(token=token)
    if existing is not None:
        _upsert_existing(existing, label, plan, force)
        return
    _insert_new(prov, fields, label, plan, force)


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
    if provider:
        if provider not in app_ctx.providers:
            app_ctx.err_console.print(
                f"[red]Unknown provider {provider!r}.[/red]"
            )
            raise typer.Exit(code=1)
        targets = app_ctx.store.filter_by_provider(provider)
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
            raise typer.Exit(code=1)

    if provider:
        cleared = app_ctx.store.reset_provider(provider)
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
        raise typer.Exit(code=1) from None
    except UsageError as e:
        app_ctx.err_console.print(f"[red]Could not check: {e}[/red]")
        raise typer.Exit(code=1) from None
    except ValueError as e:
        app_ctx.err_console.print(
            f"[red]Unexpected GitHub response: {e}[/red]"
        )
        raise typer.Exit(code=1) from None

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
        raise typer.Exit(code=1)

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
        raise typer.Exit(code=1) from e
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
    if provider.id != "codex":
        app_ctx = _get_ctx()
        app_ctx.err_console.print(
            "[red]--codex-home can only be used with the codex provider.[/red]"
        )
        raise typer.Exit(code=1)
    return codex_home.expanduser()


def _sidekick_codex_home(label: str) -> Path:
    """Return sidekick's private Codex auth cache dir for a label."""
    safe = _SAFE_CODEX_CACHE_NAME_RE.sub("_", label).strip("._-")
    if not safe:
        safe = "account"
    return CODEX_CACHE_DIR / safe


def _codex_source_blob(
    provider: Provider,
    source_home: Path | None,
) -> dict[str, Any] | None:
    """Read a Codex source auth blob only for the real provider."""
    if isinstance(provider, CodexProvider):
        return read_auth_blob(source_home)
    return None


def _refresh_credential_home(
    acct: Account,
    provider: Provider,
    from_codex_home: Path | None,
) -> Path | None:
    """Pick the credential home for a manual refresh."""
    del acct
    if from_codex_home is not None:
        return _normalize_codex_home(provider, from_codex_home)
    return None


def _apply_detected_credentials(
    acct: Account,
    detected: DetectedCredentials,
    provider: Provider,
    credential_home: Path | None,
) -> None:
    """Copy detected local credentials onto a saved account."""
    acct.access_token = detected.access_token
    if detected.refresh_token:
        acct.refresh_token = detected.refresh_token
    if detected.expires_at:
        acct.expires_at = detected.expires_at
    if detected.provider_account_id is not None:
        acct.provider_account_id = detected.provider_account_id
    if detected.plan and detected.plan != "unknown":
        acct.plan = detected.plan
    if detected.scopes is not None:
        acct.scopes = detected.scopes
    if provider.id == "codex":
        if detected.id_token is not None:
            acct.codex_id_token = detected.id_token
        if detected.last_refresh is not None:
            acct.codex_last_refresh = detected.last_refresh
        _write_sidekick_codex_cache(acct, provider, credential_home)


def _write_sidekick_codex_cache(
    acct: Account,
    provider: Provider,
    source_home: Path | None,
) -> bool:
    """Write sidekick's private copy of a Codex auth bundle."""
    if provider.id != "codex":
        return False
    return write_account_auth_file(
        acct,
        _sidekick_codex_home(acct.label),
        source_blob=_codex_source_blob(provider, source_home),
    )


def _require_codex_provider() -> Provider:
    """Return the configured Codex provider or exit."""
    app_ctx = _get_ctx()
    provider = app_ctx.providers.get("codex")
    if provider is None:
        app_ctx.err_console.print(
            "[red]Codex provider is not registered.[/red]"
        )
        raise typer.Exit(code=1)
    return provider


def _matching_codex_auth_blob(
    acct: Account,
    *homes: Path | None,
) -> dict[str, Any] | None:
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
    blob: dict[str, Any],
    provider: Provider,
) -> None:
    """Apply metadata from a matching Codex auth blob to ``acct``."""
    detected = CodexProvider._parse_blob(blob)
    if detected is not None:
        _apply_detected_credentials(acct, detected, provider, None)


def _resolve_provider(provider_id: str) -> Provider:
    """Resolve a provider id, raising a Typer exit on miss.

    :param provider_id: Provider id from user input.
    :return: The matching :class:`Provider`.
    """
    app_ctx = _get_ctx()
    provider = app_ctx.providers.get(provider_id)
    if provider is None:
        app_ctx.err_console.print(
            f"[red]Unknown provider {provider_id!r}. "
            f"Known: {', '.join(sorted(app_ctx.providers))}.[/red]"
        )
        raise typer.Exit(code=1)
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
        if provider.id == "claude":
            app_ctx.console.print(
                "[dim]Tip: run `sidekick-usages setup-token "
                "claude` to generate one.[/dim]"
            )
    ti = TokenInput(provider.token_pattern)
    return ti.read()


def _upsert_existing(
    existing: Account,
    label_override: str | None,
    plan: str | None,
    force: bool,
    *,
    fields: _CredentialFields | None = None,
) -> None:
    """Idempotent path: token already saved.

    :param existing: The account that already holds this token.
    :param label_override: New label requested by the user, if any.
    :param plan: Plan to apply, if any.
    :param force: Overwrite an existing target label.
    """
    app_ctx = _get_ctx()
    target = label_override or existing.label
    if target != existing.label:
        if target in app_ctx.store and not force:
            app_ctx.err_console.print(
                f"[yellow]Token already saved as "
                f"'{existing.label}', but '{target}' already "
                f"exists too. Use --force to overwrite.[/yellow]"
            )
            raise typer.Exit(code=1)
        app_ctx.store.rename(existing.label, target)
    acct = app_ctx.store.get(target)
    if acct is not None:
        if plan:
            acct.plan = plan
        if fields is not None:
            _apply_credential_fields(acct, fields)
            _write_sidekick_codex_cache(
                acct,
                _resolve_provider(acct.provider_id),
                fields.source_codex_home,
            )
        app_ctx.store.upsert(acct)
    app_ctx.store.save()
    app_ctx.console.print(
        f"[green]Token already saved as '{target}' — updated in place.[/green]"
    )


def _apply_credential_fields(
    acct: Account,
    fields: _CredentialFields,
) -> None:
    """Apply optional detected credential fields to an existing account."""
    if fields.provider_account_id is not None:
        acct.provider_account_id = fields.provider_account_id
    if fields.refresh_token is not None:
        acct.refresh_token = fields.refresh_token
    if fields.expires_at is not None:
        acct.expires_at = fields.expires_at
    if fields.scopes is not None:
        acct.scopes = fields.scopes
    if fields.codex_id_token is not None:
        acct.codex_id_token = fields.codex_id_token
    if fields.codex_last_refresh is not None:
        acct.codex_last_refresh = fields.codex_last_refresh


def _insert_new(
    provider: Provider,
    fields: _CredentialFields,
    label_override: str | None,
    plan: str | None,
    force: bool,
) -> None:
    """Fresh-token path: not yet stored.

    :param provider: Provider this token belongs to.
    :param fields: Normalized credential metadata.
    :param label_override: User-supplied label, if any.
    :param plan: Plan tag, if any.
    :param force: Overwrite an existing target label.
    """
    app_ctx = _get_ctx()
    label = label_override or app_ctx.store.generate_label(
        provider.id,
        plan or "account",
    )
    if label in app_ctx.store and not force:
        app_ctx.err_console.print(
            f"[yellow]Account '{label}' already exists. Use "
            f"--force or pass --label.[/yellow]"
        )
        raise typer.Exit(code=1)

    acct = Account(
        label=label,
        provider_id=provider.id,
        access_token=fields.token,
        provider_account_id=fields.provider_account_id,
        refresh_token=fields.refresh_token,
        expires_at=fields.expires_at,
        plan=plan or "unknown",
        scopes=fields.scopes,
        codex_id_token=fields.codex_id_token,
        codex_last_refresh=fields.codex_last_refresh,
    )

    warning: str | None = None
    try:
        provider.fetch_usage(acct, app_ctx.http)
    except AuthError as e:
        app_ctx.err_console.print(
            "[red]Token rejected by API (HTTP 401).[/red]"
        )
        raise typer.Exit(code=1) from e
    except ForbiddenError as e:
        # OAuth usage endpoint refused — likely an inference-only
        # token (e.g. ``claude setup-token``). Self-heal scopes=[]
        # so fetch_usage routes to the header probe, then retry to
        # validate that path works too. The probe also primes the
        # in-memory ``acct`` so a follow-up ``check`` returns
        # usage immediately without re-paying the discovery 403.
        if e.required_scope == _USAGE_REQUIRED_SCOPE and acct.scopes is None:
            acct.scopes = []
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
            if e.retry_after
            else "retry shortly."
        )
        warning = (
            f"API is rate-limited (HTTP 429). Token was saved anyway — {wait}"
        )
    except TransientError as e:
        warning = f"Could not validate token ({e}). Saved anyway."

    app_ctx.store.upsert(acct)
    _write_sidekick_codex_cache(acct, provider, fields.source_codex_home)
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
def _print_no_accounts(only: str | None) -> None:
    """Print the 'no accounts saved' hint.

    :param only: Provider filter that produced no results.
    """
    app_ctx = _get_ctx()
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


def _print_error_block(acct: Account, message: str) -> None:
    """Print an error panel for one account.

    :param acct: Account that errored.
    :param message: Human-readable error text.
    """
    app_ctx = _get_ctx()
    app_ctx.console.print(account_header(acct))
    app_ctx.console.print(f"  [red]{message}[/red]")


def _print_auth_error_block(acct: Account) -> None:
    """Print the 401 message with a 'refresh' hint.

    :param acct: Account whose token failed auth.
    """
    app_ctx = _get_ctx()
    provider = app_ctx.providers.get(acct.provider_id)
    display = provider.display_name if provider else acct.provider_id
    app_ctx.console.print(account_header(acct))
    app_ctx.console.print("  [red]Token expired or invalid (HTTP 401).[/red]")
    app_ctx.console.print(
        f"  Log in to {display} again, then [bold]"
        f"sidekick-usages refresh {acct.label}[/bold]."
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

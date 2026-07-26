"""Noninteractive saved-account selection command."""

import os
import shlex
from typing import Annotated, Never

import typer
from rich.text import Text

from sidekick_usages.cli.context import invocation_context
from sidekick_usages.cli.dashboard.models.use import UseActivationFailure
from sidekick_usages.cli.help import branded_command
from sidekick_usages.cli.validation import (
    validated_label,
    validated_provider,
)
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import CredentialHealth
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.daemon.control.protocol import ProtocolFailureError
from sidekick_usages.providers.claude.activation.service import (
    claude_environment_conflict,
    claude_environment_conflict_keys,
)

_SERVICE_PREPARATION_FAILURE_CODES = frozenset(
    {"service_incompatible", "service_stopping"}
)


def _command(*arguments: str) -> str:
    """Return one copy-safe command for Unix shells."""
    return shlex.join(arguments)


def _fail(
    ctx: typer.Context,
    message: str,
    action: str,
) -> Never:
    """Render one noninteractive failure and its exact next action."""
    invocation = invocation_context(ctx)
    invocation.err_console.print(Text(message, style="red"))
    next_action = Text("Next: ", style="dim")
    next_action.append(action, style="bold")
    invocation.err_console.print(next_action)
    raise typer.Exit(code=ExitCode.MANUAL_ACTION)


def _preparation_command(account: SavedAccount) -> str | None:
    """Return required interactive preparation for one saved account."""
    if not account.has_managed_authority:
        return _command("sidekick-usages", "migrate", "managed-auth")
    if account.credential_health is not CredentialHealth.LOGIN_REQUIRED:
        return None
    if account.provider_id is ProviderId.CODEX:
        return _command(
            "sidekick-usages",
            "codex",
            "login",
            str(account.label),
        )
    return _command("sidekick-usages", "migrate", "managed-auth")


def _use_command(
    provider_id: ProviderId,
    label: str,
    *,
    allow_remote_control_disconnect: bool = False,
) -> str:
    arguments = [
        "sidekick-usages",
        "use",
        provider_id.value,
        label,
    ]
    if allow_remote_control_disconnect:
        arguments.append("--allow-remote-control-disconnect")
    return _command(*arguments)


def use_cmd(
    ctx: typer.Context,
    provider: Annotated[
        str,
        typer.Argument(help="Provider id (claude or codex)."),
    ],
    label: Annotated[
        str,
        typer.Argument(help="Exact saved-account label."),
    ],
    allow_remote_control_disconnect: Annotated[
        bool,
        typer.Option(
            "--allow-remote-control-disconnect",
            help="Allow a proven Claude Remote Control disruption.",
        ),
    ] = False,
) -> None:
    """Select one saved account without prompting or installing services."""
    provider_id = validated_provider(ctx, provider)
    if (
        allow_remote_control_disconnect
        and provider_id is not ProviderId.CLAUDE
    ):
        _fail(
            ctx,
            "Remote Control disconnect approval applies only to Claude.",
            _use_command(provider_id, label),
        )
    account_label = validated_label(ctx, label)
    use = invocation_context(ctx).require_use()
    account = use.accounts.resolve(provider_id, account_label)
    if account is None:
        _fail(
            ctx,
            f"No saved {provider_id.value} account labeled '{account_label}'.",
            _command("sidekick-usages"),
        )
    preparation = _preparation_command(account)
    if preparation is not None:
        _fail(
            ctx,
            f"Account '{account.label}' needs interactive preparation.",
            preparation,
        )
    if provider_id is ProviderId.CLAUDE:
        environment_conflict = claude_environment_conflict(os.environ)
        if environment_conflict is not None:
            _fail(
                ctx,
                "This shell overrides Claude account selection.",
                _command(
                    "unset",
                    *claude_environment_conflict_keys(
                        environment_conflict
                    ),
                ),
            )
    try:
        result = use.activate(
            provider_id,
            account.account_id,
            allow_remote_control_disconnect,
        )
    except OSError, ProtocolFailureError:
        _fail(
            ctx,
            "The Sidekick supervisor is unavailable or incompatible.",
            _command("sidekick-usages", "daemon", "install"),
        )
    if isinstance(result, UseActivationFailure):
        action = (
            _command("sidekick-usages", "daemon", "install")
            if result.code in _SERVICE_PREPARATION_FAILURE_CODES
            else _command(
                "sidekick-usages",
                "doctor",
                "--provider",
                provider_id.value,
            )
        )
        _fail(
            ctx,
            f"Sidekick could not verify the {provider_id.value} activation.",
            action,
        )
    message = Text("Now using ", style="green")
    message.append(f"'{account.label}'", style="bold green")
    message.append(f" for {provider_id.value}.", style="green")
    invocation_context(ctx).console.print(message)


def register(application: typer.Typer) -> None:
    """Register the scriptable account-selection command."""
    branded_command(
        application,
        "use",
        help="Select one saved provider account without prompting.",
    )(use_cmd)

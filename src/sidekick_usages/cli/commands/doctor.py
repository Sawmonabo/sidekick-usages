"""Read-only doctor command adapter."""

from typing import Annotated, assert_never

import typer

from sidekick_usages.cli.context import (
    DoctorBlocked,
    DoctorFailed,
    DoctorReady,
    invocation_context,
)
from sidekick_usages.cli.help import branded_command
from sidekick_usages.core.types import ExitCode, ProviderId, highest_exit_code
from sidekick_usages.doctor import (
    doctor_exit_code as account_doctor_exit_code,
)
from sidekick_usages.doctor import render_doctor
from sidekick_usages.persistence.assessment import (
    doctor_exit_code as persistence_doctor_exit_code,
)


def _provider_filter(
    ctx: typer.Context,
    value: str | None,
) -> ProviderId | None:
    if value is None:
        return None
    try:
        return ProviderId(value)
    except ValueError:
        invocation_context(ctx).err_console.print(
            f"[red]Unknown provider {value!r}.[/red]"
        )
        raise typer.Exit(code=ExitCode.SYSTEM_ERROR) from None


def doctor_cmd(
    ctx: typer.Context,
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
    invocation = invocation_context(ctx)
    state = invocation.require_doctor(ctx).state
    provider_filter = _provider_filter(ctx, provider_id)
    if isinstance(state, DoctorBlocked):
        render_doctor(
            [],
            invocation.console,
            json_output=json_output,
            persistence=state.assessment,
        )
        code = persistence_doctor_exit_code(state.assessment.code)
        if code:
            raise typer.Exit(code=code)
        return
    if isinstance(state, DoctorFailed):
        render_doctor(
            [],
            invocation.console,
            json_output=json_output,
            persistence_failure=state.failure,
        )
        code = persistence_doctor_exit_code(state.failure.code)
        if code:
            raise typer.Exit(code=code)
        return
    if isinstance(state, DoctorReady):
        diagnostics = state.service.diagnostics(
            provider_id=provider_filter,
            label=label,
        )
        if not diagnostics:
            if state.assessment.account_count == 0:
                render_doctor(
                    [],
                    invocation.console,
                    json_output=json_output,
                    persistence=state.assessment,
                )
                code = persistence_doctor_exit_code(state.assessment.code)
                if code:
                    raise typer.Exit(code=code)
                return
            invocation.err_console.print(
                "[yellow]No matching accounts.[/yellow]"
            )
            raise typer.Exit(code=ExitCode.MANUAL_ACTION)
        render_doctor(
            diagnostics,
            invocation.console,
            json_output=json_output,
            persistence=state.assessment,
        )
        code = highest_exit_code(
            account_doctor_exit_code(diagnostics),
            persistence_doctor_exit_code(state.assessment.code),
        )
        if code:
            raise typer.Exit(code=code)
        return
    assert_never(state)


def register(application: typer.Typer) -> None:
    """Register the doctor command exactly once."""
    branded_command(application, "doctor")(doctor_cmd)


__all__ = ["register"]

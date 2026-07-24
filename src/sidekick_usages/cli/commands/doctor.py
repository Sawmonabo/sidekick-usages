"""Read-only doctor command adapter."""

import json
from typing import Annotated, assert_never

import typer

from sidekick_usages.cli.context import (
    DoctorFailed,
    DoctorReady,
    InvocationContext,
    invocation_context,
)
from sidekick_usages.cli.help import branded_command
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.doctor.service import (
    DoctorFailedResult,
    DoctorReadyResult,
    DoctorResult,
    doctor_exit_code,
    doctor_json,
    render_doctor,
)
from sidekick_usages.persistence.errors import (
    exit_code_for_persistence_code,
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


def _write_result(
    invocation: InvocationContext,
    result: DoctorResult,
    *,
    json_output: bool,
) -> None:
    """Write one completed result through the selected presentation."""
    if json_output:
        invocation.console.print(
            json.dumps(doctor_json(result), indent=2),
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        return
    invocation.console.print(
        render_doctor(result, width=invocation.console.size.width)
    )


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
    doctor = invocation.require_doctor(ctx)
    state = doctor.state
    provider_filter = _provider_filter(ctx, provider_id)
    if isinstance(state, DoctorFailed):
        _write_result(
            invocation,
            DoctorFailedResult(state.failure, doctor.supervisor),
            json_output=json_output,
        )
        code = exit_code_for_persistence_code(state.failure.code)
        if code:
            raise typer.Exit(code=code)
        return
    if isinstance(state, DoctorReady):
        diagnostics = state.service.diagnostics(
            provider_id=provider_filter,
            label=label,
        )
        if not diagnostics:
            if not state.service.accounts:
                _write_result(
                    invocation,
                    DoctorReadyResult(
                        (),
                        state.persistence,
                        state.refresh_state,
                        doctor.supervisor,
                    ),
                    json_output=json_output,
                )
                return
            invocation.err_console.print(
                "[yellow]No matching accounts.[/yellow]"
            )
            raise typer.Exit(code=ExitCode.MANUAL_ACTION)
        _write_result(
            invocation,
            DoctorReadyResult(
                tuple(diagnostics),
                state.persistence,
                state.refresh_state,
                doctor.supervisor,
            ),
            json_output=json_output,
        )
        code = doctor_exit_code(diagnostics)
        if code:
            raise typer.Exit(code=code)
        return
    assert_never(state)


def register(application: typer.Typer) -> None:
    """Register the doctor command exactly once."""
    branded_command(application, "doctor")(doctor_cmd)


__all__ = ["register"]

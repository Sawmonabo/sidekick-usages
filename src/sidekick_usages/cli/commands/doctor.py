"""Read-only doctor command adapter."""

import json
from dataclasses import replace
from typing import Annotated, assert_never

import typer

from sidekick_usages.cli.context import (
    InvocationContext,
    invocation_context,
)
from sidekick_usages.cli.contexts.models import DoctorFailed, DoctorReady
from sidekick_usages.cli.help import branded_command
from sidekick_usages.cli.validation import validated_provider
from sidekick_usages.core.types import ExitCode, ProviderId
from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.daemon.types.lifecycle import ServiceComponentState
from sidekick_usages.doctor.accounts.models import (
    DoctorFailedResult,
    DoctorReadyResult,
    DoctorResult,
)
from sidekick_usages.doctor.accounts.service import doctor_exit_code
from sidekick_usages.doctor.presentation.json import doctor_json
from sidekick_usages.doctor.presentation.service import render_doctor


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


def _scoped_supervisor(
    health: SupervisorHealth,
    provider_id: ProviderId | None,
) -> SupervisorHealth:
    """Remove provider-specific evidence outside the requested scope."""
    if provider_id is ProviderId.CLAUDE:
        return replace(
            health,
            broker=ServiceComponentState.NOT_REQUIRED,
            broker_failure_code=None,
        )
    return health


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
    provider_filter = (
        None if provider_id is None else validated_provider(ctx, provider_id)
    )
    doctor = invocation.require_doctor(ctx)
    state = doctor.state
    capabilities = doctor.capabilities.report(provider_filter)
    supervisor = _scoped_supervisor(
        doctor.supervisor,
        provider_filter,
    )
    if isinstance(state, DoctorFailed):
        result = DoctorFailedResult(
            failure=state.failure,
            supervisor=supervisor,
            capabilities=capabilities,
        )
        _write_result(
            invocation,
            result,
            json_output=json_output,
        )
        code = doctor_exit_code(result)
        if code:
            raise typer.Exit(code=code)
        return
    if isinstance(state, DoctorReady):
        diagnostics = state.service.diagnostics(
            provider_id=provider_filter,
            label=label,
        )
        scheduled_operations = state.service.scheduled_operations(
            provider_id=provider_filter,
            label=label,
        )
        unfinished_activations = state.service.unfinished_activations(
            provider_id=provider_filter,
            label=label,
        )
        if (
            label is not None
            and not diagnostics
            and not scheduled_operations
            and not unfinished_activations
        ):
            invocation.err_console.print(
                "[yellow]No matching accounts.[/yellow]"
            )
            raise typer.Exit(code=ExitCode.MANUAL_ACTION)
        result = DoctorReadyResult(
            diagnostics=tuple(diagnostics),
            scheduled_operations=scheduled_operations,
            unfinished_activations=unfinished_activations,
            persistence=state.persistence,
            refresh_state=state.refresh_state,
            supervisor=supervisor,
            capabilities=capabilities,
        )
        _write_result(
            invocation,
            result,
            json_output=json_output,
        )
        code = doctor_exit_code(result)
        if code:
            raise typer.Exit(code=code)
        return
    assert_never(state)


def register(application: typer.Typer) -> None:
    """Register the doctor command exactly once."""
    branded_command(application, "doctor")(doctor_cmd)

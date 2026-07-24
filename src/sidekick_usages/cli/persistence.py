"""CLI translation for current persistence failures."""

from typing import NoReturn

import typer

from sidekick_usages.cli.context import invocation_context
from sidekick_usages.core.types import ExitCode
from sidekick_usages.persistence.errors import (
    PersistenceError,
    exit_code_for_persistence_code,
)
from sidekick_usages.scheduler_quiescence import (
    SchedulerMutationBlockedError,
)


def exit_persistence_failure(
    ctx: typer.Context,
    error: PersistenceError | SchedulerMutationBlockedError,
) -> NoReturn:
    """Render one typed persistence failure and exit."""
    invocation = invocation_context(ctx)
    invocation.err_console.print(f"[red]{error}[/red]")
    code = (
        ExitCode.SCHEDULER_ERROR
        if isinstance(error, SchedulerMutationBlockedError)
        else exit_code_for_persistence_code(error.code)
    )
    raise typer.Exit(code=code)

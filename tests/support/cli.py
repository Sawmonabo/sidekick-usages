"""Typed CLI test composition."""

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Never

from rich.console import Console
from typer.testing import CliRunner, Result

from sidekick_usages.cli.app import create_app
from sidekick_usages.cli.context import InvocationContext
from sidekick_usages.cli.contexts.models import (
    AppContext,
    Composed,
    DaemonContext,
    DoctorContext,
    InvocationComposers,
    MigrationContext,
    PersistenceContext,
    UpdateContext,
)
from sidekick_usages.cli.contexts.use import UseContext

_CLI_APP = create_app()


def _unexpected_composition() -> Never:
    raise AssertionError("Command crossed an unconfigured composition path.")


def _fixed_composer[T](value: T) -> Callable[[], Composed[T]]:
    def compose() -> Composed[T]:
        return Composed(value, ExitStack())

    return compose


@dataclass(slots=True)
class CliHarness:
    """Invoke the CLI with fresh, explicitly configured typed composers."""

    console: Console
    err_console: Console
    application: AppContext | None = None
    persistence: PersistenceContext | None = None
    doctor: DoctorContext | None = None
    daemon: DaemonContext | None = None
    migration: MigrationContext | None = None
    update: UpdateContext | None = None
    use: UseContext | None = None

    def invoke(
        self,
        arguments: list[str],
        *,
        input_text: str | None = None,
    ) -> Result:
        """Invoke with new presentation and composition state."""
        return CliRunner().invoke(
            _CLI_APP,
            arguments,
            input=input_text,
            obj=InvocationContext(
                console=self.console,
                err_console=self.err_console,
                composers=InvocationComposers(
                    application=(
                        _unexpected_composition
                        if self.application is None
                        else _fixed_composer(self.application)
                    ),
                    persistence=(
                        _unexpected_composition
                        if self.persistence is None
                        else _fixed_composer(self.persistence)
                    ),
                    doctor=(
                        _unexpected_composition
                        if self.doctor is None
                        else _fixed_composer(self.doctor)
                    ),
                    daemon=(
                        _unexpected_composition
                        if self.daemon is None
                        else _fixed_composer(self.daemon)
                    ),
                    migration=(
                        _unexpected_composition
                        if self.migration is None
                        else _fixed_composer(self.migration)
                    ),
                    update=(
                        _unexpected_composition
                        if self.update is None
                        else _fixed_composer(self.update)
                    ),
                ),
                use_composer=self._compose_use,
            ),
        )

    def _compose_use(self) -> UseContext:
        use = self.use
        if use is None:
            raise AssertionError(
                "Command crossed an unconfigured composition path."
            )
        return use

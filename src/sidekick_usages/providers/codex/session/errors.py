"""Typed failures for neutral Codex session preparation."""

from sidekick_usages.core.selection.types import SelectionCode
from sidekick_usages.errors import UsageError
from sidekick_usages.providers.codex.session.models import (
    CodexSessionPreparationReport,
)


class CodexSessionConfigurationError(UsageError):
    """A neutral Codex session needs explicit operator recovery."""

    def __init__(self, report: CodexSessionPreparationReport) -> None:
        self.report = report
        super().__init__("The neutral Codex session requires preparation.")


class CodexRelayError(RuntimeError):
    """Typed relay failure containing only one safe selection code."""

    def __init__(self, code: SelectionCode) -> None:
        self.code = code
        super().__init__(code.value)

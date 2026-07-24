"""Bounded native command execution for service integrations."""

import subprocess

from sidekick_usages.daemon.lifecycle.errors import ServiceLifecycleError
from sidekick_usages.daemon.models.lifecycle import CommandResult
from sidekick_usages.daemon.types.lifecycle import ServiceFailureCode

__all__ = ["SystemCommandRunner"]

_COMMAND_TIMEOUT_SECONDS = 20.0


class SystemCommandRunner:
    """Run one exact native argv without shell or inherited input."""

    def run(self, argv: tuple[str, ...]) -> CommandResult:
        """Run a bounded command and capture its text result."""
        if not argv or any(
            not argument or "\0" in argument for argument in argv
        ):
            raise ServiceLifecycleError(ServiceFailureCode.COMMAND_FAILED)
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                text=True,
                capture_output=True,
                check=False,
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        except OSError, subprocess.TimeoutExpired:
            raise ServiceLifecycleError(
                ServiceFailureCode.COMMAND_FAILED
            ) from None
        try:
            return CommandResult(
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
        except ValueError:
            raise ServiceLifecycleError(
                ServiceFailureCode.COMMAND_FAILED
            ) from None

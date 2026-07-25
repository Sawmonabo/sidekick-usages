"""Secret-safe Claude process failures."""

from sidekick_usages.errors import UsageError
from sidekick_usages.providers.claude.types import ClaudeProcessFailure

_PROCESS_FAILURE_MESSAGES = {
    ClaudeProcessFailure.PROCESS_UNAVAILABLE: (
        "The Claude process is unavailable."
    ),
    ClaudeProcessFailure.PROCESS_UNSAFE: "The Claude command is unsafe.",
    ClaudeProcessFailure.OUTPUT_UNREADABLE: (
        "The Claude process output could not be read safely."
    ),
    ClaudeProcessFailure.TIMED_OUT: (
        "The Claude process exceeded its time limit."
    ),
}


class ClaudeProcessError(UsageError):
    """One bounded Claude process failure containing no provider output."""

    def __init__(self, code: ClaudeProcessFailure) -> None:
        self.code = code
        super().__init__(_PROCESS_FAILURE_MESSAGES[code])

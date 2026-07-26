"""Secret-safe Claude provider failures."""

from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import UsageError
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureCause,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.types import ClaudeProcessFailure

_PROCESS_FAILURE_MESSAGES = {
    ClaudeProcessFailure.PROCESS_UNAVAILABLE: (
        "The Claude process is unavailable."
    ),
    ClaudeProcessFailure.PROCESS_UNSAFE: "The Claude command is unsafe.",
    ClaudeProcessFailure.OUTPUT_TOO_LARGE: (
        "The Claude process output exceeded its safe limit."
    ),
    ClaudeProcessFailure.OUTPUT_UNREADABLE: (
        "The Claude process output could not be read safely."
    ),
    ClaudeProcessFailure.TIMED_OUT: (
        "The Claude process exceeded its time limit."
    ),
}


def claude_failure(
    kind: ProviderFailureKind,
    message: str,
    *,
    cause: ProviderFailureCause | None = None,
    action_required: bool = True,
    fields: tuple[str, ...] = (),
) -> ProviderFailure:
    """Build one secret-safe Claude provider failure."""
    return ProviderFailure(
        provider_id=ProviderId.CLAUDE,
        kind=kind,
        message=message,
        cause=cause,
        action_required=action_required,
        fields=fields,
    )


class ClaudeProcessError(UsageError):
    """One bounded Claude process failure containing no provider output."""

    def __init__(self, code: ClaudeProcessFailure) -> None:
        self.code = code
        super().__init__(_PROCESS_FAILURE_MESSAGES[code])

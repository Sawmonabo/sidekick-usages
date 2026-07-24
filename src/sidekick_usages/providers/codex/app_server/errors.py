"""Secret-safe Codex app-server failures."""

from sidekick_usages.errors import UsageError
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)

_FAILURE_MESSAGES = {
    CodexAppServerFailure.EXECUTABLE_MISSING: (
        "The Codex CLI executable was not found."
    ),
    CodexAppServerFailure.EXECUTABLE_UNSAFE: (
        "The Codex CLI executable changed or is unsafe."
    ),
    CodexAppServerFailure.VERSION_UNSUPPORTED: (
        "The installed Codex CLI version is not supported."
    ),
    CodexAppServerFailure.CAPABILITY_UNSUPPORTED: (
        "The installed Codex app server lacks required authentication "
        "capabilities."
    ),
    CodexAppServerFailure.PROCESS_FAILED: (
        "The Codex app-server process failed."
    ),
    CodexAppServerFailure.PROCESS_TIMEOUT: (
        "The Codex app-server process exceeded its time limit."
    ),
    CodexAppServerFailure.PROTOCOL_MALFORMED: (
        "The Codex app server returned an invalid protocol message."
    ),
    CodexAppServerFailure.REQUEST_REJECTED: (
        "The Codex app server rejected the requested operation."
    ),
    CodexAppServerFailure.PROTOCOL_TIMEOUT: (
        "The Codex app server did not respond in time."
    ),
    CodexAppServerFailure.PROTOCOL_CLOSED: (
        "The Codex app-server connection closed unexpectedly."
    ),
}


class CodexAppServerError(UsageError):
    """One typed app-server failure containing no provider output."""

    def __init__(self, code: CodexAppServerFailure) -> None:
        self.code = code
        super().__init__(_FAILURE_MESSAGES[code])

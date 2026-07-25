"""Secret-safe operating-system boundary failures."""

from sidekick_usages.errors import UsageError
from sidekick_usages.platform.types import ExecutableFailure

_EXECUTABLE_MESSAGES = {
    ExecutableFailure.MISSING: "The requested executable was not found.",
    ExecutableFailure.UNSAFE: "The requested executable is unsafe.",
}


class ExecutableQualificationError(UsageError):
    """One exact executable could not be qualified safely."""

    def __init__(self, code: ExecutableFailure) -> None:
        self.code = code
        super().__init__(_EXECUTABLE_MESSAGES[code])

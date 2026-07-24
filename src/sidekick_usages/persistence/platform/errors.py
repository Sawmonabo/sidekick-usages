"""Native persistence failures."""

from sidekick_usages.persistence.platform.types import NativeFailureKind


class NativeFilesystemError(Exception):
    """An input-free native failure awaiting persistence translation."""

    def __init__(self, kind: NativeFailureKind) -> None:
        self.kind = kind
        super().__init__(f"Native persistence operation failed: {kind}.")

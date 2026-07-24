"""Closed values for protected credential persistence."""

from enum import StrEnum


class StoredCredentialKind(StrEnum):
    """Closed protected credential variants."""

    CLAUDE_SETUP = "claude_setup"
    CLAUDE_LOGIN = "claude_login"
    CODEX_LOGIN = "codex_login"


class PrivateCredentialState(StrEnum):
    """Qualified presence state for the protected credential root."""

    ABSENT = "absent"
    PRESENT = "present"
    INTERRUPTED = "interrupted"


class PrivateCredentialOwnership(StrEnum):
    """Lexical ownership of a requested private bundle."""

    CANONICAL = "canonical"
    EXTERNAL = "external"

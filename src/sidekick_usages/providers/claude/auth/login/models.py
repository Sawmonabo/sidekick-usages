"""Immutable official Claude login models and outcomes."""

from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.core.accounts.validation import (
    MAX_METADATA_BYTES,
    require_bounded_text,
)


class ClaudeOfficialLoginResult(StrEnum):
    """Secret-safe outcome from one official Claude login process."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ClaudeAuthStatus:
    """Bounded non-secret state from the official auth-status command."""

    return_code: int
    logged_in: bool
    auth_method: str
    api_provider: str
    email: str | None = None
    organization_id: str | None = None
    organization_name: str | None = None
    subscription_type: str | None = None

    def __post_init__(self) -> None:
        """Require every provider-returned identity field to remain bounded."""
        for name, value in (
            ("Claude auth method", self.auth_method),
            ("Claude API provider", self.api_provider),
            ("Claude account email", self.email),
            ("Claude organization ID", self.organization_id),
            ("Claude organization name", self.organization_name),
            ("Claude subscription type", self.subscription_type),
        ):
            if value is not None:
                require_bounded_text(
                    value,
                    name=name,
                    maximum=MAX_METADATA_BYTES,
                )

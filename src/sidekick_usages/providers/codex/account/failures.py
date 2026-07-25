"""Provider failure mapping for strict Codex account reads."""

from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.account.types import (
    CodexAccountReadFailure,
)

_FAILURE_KINDS = {
    CodexAccountReadFailure.MISSING: ProviderFailureKind.MISSING,
    CodexAccountReadFailure.MALFORMED: ProviderFailureKind.MALFORMED,
    CodexAccountReadFailure.UNSUPPORTED: ProviderFailureKind.UNSUPPORTED,
}
_FAILURE_MESSAGES = {
    CodexAccountReadFailure.MISSING: ("The managed Codex home is logged out."),
    CodexAccountReadFailure.MALFORMED: (
        "Codex returned malformed account metadata."
    ),
    CodexAccountReadFailure.UNSUPPORTED: (
        "The managed Codex home is not a ChatGPT account."
    ),
}


def codex_account_provider_failure(
    failure: CodexAccountReadFailure,
) -> ProviderFailure:
    """Translate one account-read outcome to provider vocabulary."""
    return ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=_FAILURE_KINDS[failure],
        message=_FAILURE_MESSAGES[failure],
    )

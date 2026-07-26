"""Provider failure mapping for strict Codex account reads."""

from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.account.types import (
    CodexAccountReadFailure,
)
from sidekick_usages.providers.codex.failures import codex_failure

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
    return codex_failure(
        _FAILURE_KINDS[failure],
        _FAILURE_MESSAGES[failure],
    )

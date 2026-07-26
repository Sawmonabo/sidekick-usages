"""Secret-safe Codex provider failures."""

from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)


def codex_failure(
    kind: ProviderFailureKind,
    message: str,
    *,
    fields: tuple[str, ...] = (),
) -> ProviderFailure:
    """Build one secret-safe Codex provider failure."""
    return ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=kind,
        message=message,
        fields=fields,
    )

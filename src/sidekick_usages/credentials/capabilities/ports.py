"""Read-only provider capability evidence boundary."""

from typing import Protocol

from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.capabilities.models import (
    ProviderCapabilityReport,
)


class ProviderCapabilityEvidenceSource(Protocol):
    """Provide cached capability readiness and scoped evidence."""

    def ready(self, provider_id: ProviderId) -> bool:
        """Return whether one provider capability gate passed."""

    def report(
        self,
        provider_id: ProviderId | None = None,
    ) -> ProviderCapabilityReport:
        """Return evidence for one provider or every provider."""

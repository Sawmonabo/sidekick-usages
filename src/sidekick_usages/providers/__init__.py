"""Provider registry.

Adding a provider means importing it here and adding it to the explicit
registry factory.
"""

from sidekick_usages.clock import Clock
from sidekick_usages.core.models import DetectedCredentials
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.claude import ClaudeProvider
from sidekick_usages.providers.codex import (
    CodexProvider,
    PrivateAuthBundleWriter,
)


def build_provider_registry(
    clock: Clock,
    private_auth_writer: PrivateAuthBundleWriter | None = None,
) -> dict[ProviderId, Provider]:
    """Build providers that share one application wall clock."""
    # Insertion order controls the default rendering order.
    return {
        ProviderId.CLAUDE: ClaudeProvider(clock),
        ProviderId.CODEX: CodexProvider(clock, private_auth_writer),
    }


__all__ = [
    "DetectedCredentials",
    "Provider",
    "build_provider_registry",
]

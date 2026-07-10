"""Provider registry.

Adding a provider means importing it here and adding it to the explicit
registry factory.
"""

from sidekick_usages.clock import Clock
from sidekick_usages.providers.base import (
    DetectedCredentials,
    Provider,
)
from sidekick_usages.providers.claude import ClaudeProvider
from sidekick_usages.providers.codex import CodexProvider


def build_provider_registry(clock: Clock) -> dict[str, Provider]:
    """Build providers that share one application wall clock."""
    # Insertion order controls the default rendering order.
    return {
        "claude": ClaudeProvider(clock),
        "codex": CodexProvider(clock),
    }


__all__ = [
    "DetectedCredentials",
    "Provider",
    "build_provider_registry",
]

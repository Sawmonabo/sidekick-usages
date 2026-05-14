"""Provider registry.

Importing this module gives you a name -> instance map. Adding a
new provider means importing it here and adding to ``PROVIDERS``.
"""

from sidekick_usages.providers.base import (
    DetectedCredentials,
    Provider,
)
from sidekick_usages.providers.claude import ClaudeProvider
from sidekick_usages.providers.codex import CodexProvider

# Insertion order matters: it controls the default rendering order
# when listing across providers.
PROVIDERS: dict[str, Provider] = {
    "claude": ClaudeProvider(),
    "codex": CodexProvider(),
}


def get_provider(provider_id: str) -> Provider:
    """Look up a provider by id.

    :param provider_id: Provider id (``"claude"`` or ``"codex"``).
    :return: The matching ``Provider`` instance.
    :raises KeyError: When the id is not registered.
    """
    if provider_id not in PROVIDERS:
        raise KeyError(
            f"Unknown provider {provider_id!r}. Known: {', '.join(PROVIDERS)}."
        )
    return PROVIDERS[provider_id]


__all__ = [
    "PROVIDERS",
    "DetectedCredentials",
    "Provider",
    "get_provider",
]

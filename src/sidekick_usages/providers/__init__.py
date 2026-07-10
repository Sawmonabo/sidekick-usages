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
__all__ = [
    "PROVIDERS",
    "DetectedCredentials",
    "Provider",
]

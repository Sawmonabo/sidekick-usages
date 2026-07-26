"""Provider capability evidence unions."""

from sidekick_usages.providers.claude.managed.models import (
    ClaudeRuntimeCapabilities,
)
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedFailure,
)
from sidekick_usages.providers.claude.models import ClaudeExecutable
from sidekick_usages.providers.codex.app_server.models import (
    CodexAppServerCapabilities,
    CodexExecutable,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)

type ProviderCapabilityEvidence = (
    ClaudeRuntimeCapabilities | CodexAppServerCapabilities
)
type ProviderCapabilityFailure = ClaudeManagedFailure | CodexAppServerFailure
type ProviderExecutable = ClaudeExecutable | CodexExecutable

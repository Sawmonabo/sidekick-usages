"""Heartbeat provider registry."""

from collections.abc import Mapping

from sidekick_usages.core.types import ProviderId
from sidekick_usages.heartbeat.base import HeartbeatProvider
from sidekick_usages.heartbeat.claude import ClaudeHeartbeat
from sidekick_usages.heartbeat.codex import CodexHeartbeat
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.codex import CodexProvider


def build_heartbeat_registry(
    providers: Mapping[ProviderId, Provider],
) -> dict[ProviderId, HeartbeatProvider]:
    """Build heartbeat adapters from the composed provider registry."""
    codex = providers.get(ProviderId.CODEX)
    if not isinstance(codex, CodexProvider):
        raise TypeError("Codex heartbeat requires the Codex provider.")
    return {
        ProviderId.CLAUDE: ClaudeHeartbeat(),
        ProviderId.CODEX: CodexHeartbeat(codex),
    }

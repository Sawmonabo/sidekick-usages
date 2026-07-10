"""Explicit composition of the two supported provider integrations."""

from collections.abc import Mapping

from sidekick_usages.clock import Clock
from sidekick_usages.core.types import ProviderId
from sidekick_usages.heartbeat.ports import HeartbeatProvider
from sidekick_usages.providers.base import Provider
from sidekick_usages.providers.claude.heartbeat import ClaudeHeartbeat
from sidekick_usages.providers.claude.provider import ClaudeProvider
from sidekick_usages.providers.codex.heartbeat import CodexHeartbeat
from sidekick_usages.providers.codex.provider import CodexProvider


def build_provider_registry(
    clock: Clock,
) -> dict[ProviderId, Provider]:
    """Build the closed provider registry in display order."""
    return {
        ProviderId.CLAUDE: ClaudeProvider(clock),
        ProviderId.CODEX: CodexProvider(clock),
    }


def build_heartbeat_registry(
    providers: Mapping[ProviderId, Provider],
) -> dict[ProviderId, HeartbeatProvider]:
    """Build provider-owned heartbeat adapters from composed providers."""
    heartbeats: dict[ProviderId, HeartbeatProvider] = {}
    if ProviderId.CLAUDE in providers:
        heartbeats[ProviderId.CLAUDE] = ClaudeHeartbeat()
    codex = providers.get(ProviderId.CODEX)
    if codex is not None and not isinstance(codex, CodexProvider):
        raise TypeError("Codex heartbeat requires the Codex provider.")
    if codex is not None:
        heartbeats[ProviderId.CODEX] = CodexHeartbeat(codex)
    return heartbeats


__all__ = ["build_heartbeat_registry", "build_provider_registry"]

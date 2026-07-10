"""Explicit provider composition contract."""

from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.claude.heartbeat import ClaudeHeartbeat
from sidekick_usages.providers.claude.provider import ClaudeProvider
from sidekick_usages.providers.codex.heartbeat import CodexHeartbeat
from sidekick_usages.providers.codex.provider import CodexProvider
from sidekick_usages.providers.registry import (
    build_heartbeat_registry,
    build_provider_registry,
)
from tests.test_support import FixedClock


def test_registry_composes_exactly_two_provider_owned_integrations() -> None:
    """Composition is closed and preserves explicit capability subsets."""
    providers = build_provider_registry(FixedClock())
    heartbeats = build_heartbeat_registry(providers)

    assert tuple(providers) == (ProviderId.CLAUDE, ProviderId.CODEX)
    assert isinstance(providers[ProviderId.CLAUDE], ClaudeProvider)
    assert isinstance(providers[ProviderId.CODEX], CodexProvider)
    assert tuple(heartbeats) == (ProviderId.CLAUDE, ProviderId.CODEX)
    assert isinstance(heartbeats[ProviderId.CLAUDE], ClaudeHeartbeat)
    assert isinstance(heartbeats[ProviderId.CODEX], CodexHeartbeat)
    assert build_heartbeat_registry({}) == {}
    assert tuple(
        build_heartbeat_registry(
            {ProviderId.CLAUDE: providers[ProviderId.CLAUDE]}
        )
    ) == (ProviderId.CLAUDE,)
    assert tuple(
        build_heartbeat_registry(
            {ProviderId.CODEX: providers[ProviderId.CODEX]}
        )
    ) == (ProviderId.CODEX,)

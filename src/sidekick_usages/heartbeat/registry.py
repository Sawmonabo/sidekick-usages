"""Heartbeat provider registry."""

from sidekick_usages.heartbeat.base import HeartbeatProvider
from sidekick_usages.heartbeat.claude import ClaudeHeartbeat
from sidekick_usages.heartbeat.codex import CodexHeartbeat

HEARTBEAT_PROVIDERS: dict[str, HeartbeatProvider] = {
    "claude": ClaudeHeartbeat(),
    "codex": CodexHeartbeat(),
}

"""Behavioral parity tests for Claude Code token activity."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from sidekick_usages.core.models import (
    TokenActivitySummary,
    TokenActivityUnavailable,
)
from sidekick_usages.core.types import TokenActivityScope
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.activity import (
    ClaudeActivity,
    discover_claude_config_dir,
)

REFERENCE_TIME = datetime(2026, 7, 10, 18, tzinfo=UTC)


def _assistant(
    input_tokens: int,
    output_tokens: int,
    *,
    sidechain: bool = False,
    timestamp: str = "2026-07-10T12:00:00Z",
) -> dict[str, object]:
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "isSidechain": sidechain,
        "message": {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 8_000_000_000,
                "cache_creation_input_tokens": 9_000_000_000,
            }
        },
    }


def _write_lines(path: Path, *records: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )


def test_live_total_matches_claude_and_changes_without_cache_rewrite(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "stats-cache.json"
    cache.write_text(
        json.dumps(
            {
                "lastComputedDate": "2026-07-10",
                "firstSessionDate": "2025-12-28T23:26:31.884Z",
                "modelUsage": {
                    "test-model": {
                        "inputTokens": 230_254_503,
                        "outputTokens": 671_226_575,
                        "cacheReadInputTokens": 80_000_000_000,
                        "cacheCreationInputTokens": 90_000_000_000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    transcript = tmp_path / "projects" / "project" / "session.jsonl"
    _write_lines(
        transcript,
        {"type": "user", "message": "synthetic fixture"},
        _assistant(733_122, 1_249_885),
    )
    original_cache = cache.read_bytes()
    activity = ClaudeActivity(tmp_path)

    assert activity.read(REFERENCE_TIME) == TokenActivitySummary(
        total_tokens=903_464_085,
        scope=TokenActivityScope.LOCAL_INSTALLATION,
        since=date(2025, 12, 28),
    )

    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(f"{json.dumps(_assistant(7, 11))}\n")

    assert activity.read(REFERENCE_TIME) == TokenActivitySummary(
        total_tokens=903_464_103,
        scope=TokenActivityScope.LOCAL_INSTALLATION,
        since=date(2025, 12, 28),
    )
    assert cache.read_bytes() == original_cache


def test_parent_sidechain_copy_is_excluded_but_subagent_is_included(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "projects" / "project" / "session.jsonl"
    subagent = (
        tmp_path
        / "projects"
        / "project"
        / "session"
        / "subagents"
        / "agent-test.jsonl"
    )
    _write_lines(
        parent,
        _assistant(1, 2),
        _assistant(100, 200, sidechain=True),
    )
    _write_lines(subagent, _assistant(10, 20, sidechain=True))

    assert ClaudeActivity(tmp_path).read(REFERENCE_TIME) == (
        TokenActivitySummary(
            total_tokens=33,
            scope=TokenActivityScope.LOCAL_INSTALLATION,
            since=date(2026, 7, 10),
        )
    )


def test_source_states_remain_explicit_and_active_final_fragment_is_safe(
    tmp_path: Path,
) -> None:
    assert discover_claude_config_dir(
        {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    ) == tmp_path
    activity = ClaudeActivity(tmp_path)
    assert activity.read(REFERENCE_TIME) == TokenActivityUnavailable(
        scope=TokenActivityScope.LOCAL_INSTALLATION
    )

    transcript = tmp_path / "projects" / "project" / "session.jsonl"
    _write_lines(transcript, _assistant(2, 3))
    with transcript.open("ab") as stream:
        stream.write(b'{"type":"assistant"')
    assert activity.read(REFERENCE_TIME) == TokenActivitySummary(
        total_tokens=5,
        scope=TokenActivityScope.LOCAL_INSTALLATION,
        since=date(2026, 7, 10),
    )

    with transcript.open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(ProviderBoundaryError) as malformed:
        activity.read(REFERENCE_TIME)
    assert malformed.value.failure.kind is ProviderFailureKind.MALFORMED

    transcript.unlink()
    (tmp_path / "stats-cache.json").mkdir()
    with pytest.raises(ProviderBoundaryError) as unreadable:
        activity.read(REFERENCE_TIME)
    assert unreadable.value.failure.kind is ProviderFailureKind.UNREADABLE

"""Behavioral tests for typed lifetime collection outcomes."""

import json
import os
from datetime import date
from pathlib import Path

import pytest

from sidekick_usages import lifetime
from sidekick_usages.lifetime import (
    LifetimeFailure,
    LifetimeFailureKind,
    LifetimeTotal,
    LifetimeUnavailable,
)
from tests.test_support import make_application_paths


def _rollout(directory: Path, source_date: str, outputs: list[int]) -> Path:
    """Create one minimal Codex rollout with cumulative output counts."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-{source_date}T00-00-00-abc.jsonl"
    lines = [
        json.dumps(
            {
                "payload": {
                    "info": {"total_token_usage": {"output_tokens": output}}
                }
            }
        )
        for output in outputs
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(("outputs", "expected"), [([], 0), ([100, 200], 300)])
def test_claude_preserves_valid_zero_and_nonzero_totals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outputs: list[int],
    expected: int,
) -> None:
    stats = tmp_path / "stats-cache.json"
    stats.write_text(
        json.dumps(
            {
                "firstSessionDate": "2025-12-28T00:00:00Z",
                "modelUsage": {
                    f"model-{index}": {"outputTokens": output}
                    for index, output in enumerate(outputs)
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(lifetime, "_CLAUDE_STATS_FILE", stats)

    assert lifetime.claude_lifetime_output() == LifetimeTotal(
        expected,
        date(2025, 12, 28),
    )


def test_claude_distinguishes_missing_unreadable_and_malformed_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stats = tmp_path / "stats-cache.json"
    monkeypatch.setattr(lifetime, "_CLAUDE_STATS_FILE", stats)

    assert lifetime.claude_lifetime_output() == LifetimeUnavailable()

    stats.mkdir()
    assert lifetime.claude_lifetime_output() == LifetimeFailure(
        LifetimeFailureKind.SOURCE_UNREADABLE
    )

    stats.rmdir()
    stats.write_text(
        '{"modelUsage": {}, "firstSessionDate": "not-a-date"}',
        encoding="utf-8",
    )
    assert lifetime.claude_lifetime_output() == LifetimeFailure(
        LifetimeFailureKind.SOURCE_MALFORMED
    )


def test_codex_preserves_a_valid_zero_on_a_cold_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    _rollout(sessions, "2026-03-30", [0])
    cache_file = make_application_paths(tmp_path).lifetime_cache_file
    monkeypatch.setattr(lifetime, "_CODEX_SESSIONS_DIR", sessions)

    assert lifetime.codex_lifetime_output(cache_file) == LifetimeTotal(
        0,
        date(2026, 3, 30),
    )
    assert cache_file.is_file()


def test_codex_sums_each_rollouts_maximum_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    first = _rollout(
        sessions / "first",
        "2026-03-30",
        [10, 50, 30],
    )
    second = _rollout(
        sessions / "second",
        "2026-03-30",
        [5, 200],
    )
    first_stat = first.stat()
    os.utime(
        second,
        ns=(first_stat.st_atime_ns, first_stat.st_mtime_ns),
    )
    monkeypatch.setattr(lifetime, "_CODEX_SESSIONS_DIR", sessions)

    cache_file = tmp_path / "cache.json"
    result = lifetime.codex_lifetime_output(cache_file)

    assert result == LifetimeTotal(250, date(2026, 3, 30))
    assert lifetime.codex_lifetime_output(cache_file) == result


def test_codex_distinguishes_missing_and_invalid_corpus_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(lifetime, "_CODEX_SESSIONS_DIR", sessions)

    assert (
        lifetime.codex_lifetime_output(tmp_path / "cache.json")
        == LifetimeUnavailable()
    )

    sessions.write_text("not a directory", encoding="utf-8")
    assert lifetime.codex_lifetime_output(
        tmp_path / "cache.json"
    ) == LifetimeFailure(LifetimeFailureKind.SOURCE_UNREADABLE)


def test_codex_distinguishes_invalid_and_unreadable_existing_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    _rollout(sessions, "2026-03-30", [42])
    cache_file = tmp_path / "cache.json"
    monkeypatch.setattr(lifetime, "_CODEX_SESSIONS_DIR", sessions)

    cache_file.write_text("{}", encoding="utf-8")
    assert lifetime.codex_lifetime_output(cache_file) == LifetimeFailure(
        LifetimeFailureKind.CACHE_READ_FAILED
    )

    cache_file.unlink()
    cache_file.mkdir()
    assert lifetime.codex_lifetime_output(cache_file) == LifetimeFailure(
        LifetimeFailureKind.CACHE_READ_FAILED
    )


def test_codex_uses_an_unchanged_rollouts_cached_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    rollout = _rollout(sessions, "2026-03-30", [42])
    cache_file = make_application_paths(tmp_path).lifetime_cache_file
    monkeypatch.setattr(lifetime, "_CODEX_SESSIONS_DIR", sessions)

    expected = LifetimeTotal(42, date(2026, 3, 30))
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(
        json.dumps(
            {
                "files": {
                    rollout.name: {
                        "mtime": rollout.stat().st_mtime,
                        "output": 999,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert lifetime.codex_lifetime_output(cache_file) == expected
    assert json.loads(cache_file.read_text(encoding="utf-8")) == {
        "files": {
            rollout.name: {
                "mtime_ns": rollout.stat().st_mtime_ns,
                "output": 42,
            }
        }
    }

    stat = rollout.stat()
    rollout.write_text("not json\n", encoding="utf-8")
    os.utime(rollout, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert lifetime.codex_lifetime_output(cache_file) == expected


def test_codex_surfaces_cache_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    _rollout(sessions, "2026-03-30", [42])
    cache_file = tmp_path / "cache.json"
    monkeypatch.setattr(lifetime, "_CODEX_SESSIONS_DIR", sessions)

    def fail_write(
        _path: Path,
        _data: str,
        *,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        del encoding, errors, newline
        raise OSError

    monkeypatch.setattr(Path, "write_text", fail_write)

    assert lifetime.codex_lifetime_output(cache_file) == LifetimeFailure(
        LifetimeFailureKind.CACHE_WRITE_FAILED
    )


@pytest.mark.parametrize(
    "failure_kind",
    [
        LifetimeFailureKind.SOURCE_MALFORMED,
        LifetimeFailureKind.SOURCE_UNREADABLE,
    ],
)
def test_failed_rollout_never_persists_a_partial_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: LifetimeFailureKind,
) -> None:
    sessions = tmp_path / "sessions"
    _rollout(sessions, "2026-03-30", [42])
    cache_file = tmp_path / "cache.json"
    monkeypatch.setattr(lifetime, "_CODEX_SESSIONS_DIR", sessions)
    assert lifetime.codex_lifetime_output(cache_file) == LifetimeTotal(
        42,
        date(2026, 3, 30),
    )
    original_cache = cache_file.read_bytes()

    _rollout(sessions, "2026-04-01", [100])
    failed = _rollout(sessions, "2026-05-01", [200])
    if failure_kind is LifetimeFailureKind.SOURCE_UNREADABLE:
        original_stat = Path.stat

        def fail_source_stat(
            path: Path,
            *,
            follow_symlinks: bool = True,
        ) -> os.stat_result:
            if path == failed:
                raise OSError
            return original_stat(path, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(Path, "stat", fail_source_stat)
    else:
        failed.write_text("not json\n", encoding="utf-8")

    assert lifetime.codex_lifetime_output(cache_file) == LifetimeFailure(
        failure_kind
    )
    assert cache_file.read_bytes() == original_cache

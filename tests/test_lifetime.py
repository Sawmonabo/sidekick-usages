import json
import os

from sidekick_usages import lifetime
from tests.test_support import make_application_paths


def test_format_tokens():
    assert lifetime.format_tokens(0) == "0"
    assert lifetime.format_tokens(950) == "950"
    assert lifetime.format_tokens(12_300) == "12K"
    assert lifetime.format_tokens(424_000_000) == "424M"
    assert lifetime.format_tokens(1_500_000_000) == "1.5B"


def test_format_since():
    assert lifetime.format_since(None) == ""
    assert lifetime.format_since("2026-03-30") == "Mar 30"
    assert lifetime.format_since("2025-12-28T10:00:00Z") == "Dec 28"
    assert lifetime.format_since("garbage") == "garbage"


def test_claude_lifetime_output_sums_model_output(tmp_path, monkeypatch):
    stats = tmp_path / "stats-cache.json"
    stats.write_text(
        json.dumps(
            {
                "firstSessionDate": "2025-12-28T00:00:00Z",
                "modelUsage": {
                    "model-a": {"outputTokens": 100, "inputTokens": 9},
                    "model-b": {"outputTokens": 200},
                },
            }
        )
    )
    monkeypatch.setattr(lifetime, "_CLAUDE_STATS_FILE", stats)
    assert lifetime.claude_lifetime_output() == (300, "2025-12-28T00:00:00Z")


def test_claude_lifetime_output_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(lifetime, "_CLAUDE_STATS_FILE", tmp_path / "none.json")
    assert lifetime.claude_lifetime_output() == (0, None)


def test_claude_lifetime_non_dict_json_returns_zero(tmp_path, monkeypatch):
    f = tmp_path / "stats-cache.json"
    f.write_text("[]")
    monkeypatch.setattr(lifetime, "_CLAUDE_STATS_FILE", f)
    assert lifetime.claude_lifetime_output() == (0, None)


def test_load_codex_cache_non_dict_json_returns_empty(tmp_path):
    f = tmp_path / "cache.json"
    f.write_text("[1, 2, 3]")
    assert lifetime._load_codex_cache(f) == {}


def _rollout(dir_, date, outputs):
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"rollout-{date}T00-00-00-abc.jsonl"
    lines = [
        json.dumps(
            {"payload": {"info": {"total_token_usage": {"output_tokens": o}}}}
        )
        for o in outputs
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_codex_lifetime_sums_per_file_max(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    _rollout(sessions / "2026" / "03", "2026-03-30", [10, 50, 30])  # max 50
    _rollout(sessions / "2026" / "06", "2026-06-18", [5, 200])  # max 200
    monkeypatch.setattr(lifetime, "_CODEX_SESSIONS_DIR", sessions)
    assert lifetime.codex_lifetime_output(tmp_path / "c.json") == (
        250,
        "2026-03-30",
    )


def test_codex_lifetime_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(lifetime, "_CODEX_SESSIONS_DIR", tmp_path / "none")
    assert lifetime.codex_lifetime_output(tmp_path / "c.json") == (0, None)


def test_codex_lifetime_uses_cache(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    path = _rollout(sessions, "2026-03-30", [42])
    cache_file = make_application_paths(tmp_path).lifetime_cache_file
    monkeypatch.setattr(lifetime, "_CODEX_SESSIONS_DIR", sessions)

    assert lifetime.codex_lifetime_output(cache_file) == (42, "2026-03-30")
    assert cache_file.exists()
    # Corrupt the file body but keep mtime: a cache hit ignores it.
    st = path.stat()
    path.write_text("not json\n")
    os.utime(path, (st.st_atime, st.st_mtime))
    assert lifetime.codex_lifetime_output(cache_file) == (42, "2026-03-30")

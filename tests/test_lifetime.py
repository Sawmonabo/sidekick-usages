import json

from sidekick_usages import lifetime


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

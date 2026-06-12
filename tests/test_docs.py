"""Documentation coverage checks for user-facing command changes."""

from pathlib import Path


def test_heartbeat_docs_explain_commands_and_quota() -> None:
    """Heartbeat docs must cover opt-in behavior and real model calls."""
    readme = Path("README.md").read_text()
    maintenance = Path("docs/token-maintenance.md").read_text()
    heartbeat = Path("docs/heartbeat.md").read_text()

    combined = "\n".join([readme, maintenance, heartbeat])
    assert "sidekick-usages heartbeat enable" in combined
    assert "sidekick-usages heartbeat --all --quiet" in combined
    assert "sidekick-usages maintain --quiet" in combined
    assert "claude-haiku-4-5-20251001" in combined
    assert "gpt-5.4-mini" in combined
    assert "gpt-5.3-codex-spark" in combined
    assert "--target spark" in combined
    assert "real model request" in heartbeat
    assert "consumes" in heartbeat

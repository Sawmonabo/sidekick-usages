"""Documentation coverage checks for user-facing command changes."""

from pathlib import Path


def test_heartbeat_guide_owns_commands_models_and_quota() -> None:
    """The heartbeat guide must remain the detailed product contract."""
    maintenance = Path("docs/token-maintenance.md").read_text()
    heartbeat = Path("docs/heartbeat.md").read_text()
    normalized = " ".join(heartbeat.split())

    required_contracts = (
        "sidekick-usages heartbeat enable",
        "sidekick-usages heartbeat --all --quiet",
        "sidekick-usages maintain --quiet",
        "claude-haiku-4-5-20251001",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
        "--target spark",
        "real model request",
        "consumes",
    )
    for contract in required_contracts:
        assert contract in normalized
    assert "./heartbeat.md" in maintenance
    assert "gpt-5.4-mini" not in maintenance
    assert "gpt-5.3-codex-spark" not in maintenance

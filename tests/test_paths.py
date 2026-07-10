"""Behavioral tests for Sidekick-owned path composition."""

import json
from pathlib import Path

from sidekick_usages.paths import discover_application_paths
from sidekick_usages.store import AccountStore
from tests.test_support import make_application_paths


def test_discovery_preserves_current_locations_without_filesystem_side_effects(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Discovery returns the compatibility contract without creating it."""
    home = tmp_path / "absent-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))

    paths = discover_application_paths()

    sidekick_root = home / ".config" / "sidekick-usages"
    assert paths.accounts.canonical == sidekick_root / "accounts.json"
    assert paths.accounts.existing_sidekick == paths.accounts.canonical
    assert paths.accounts.prototype_cc_usage == (
        home / ".config" / "cc-usage" / "accounts.json"
    )
    assert paths.accounts.prototype_cc_usage != paths.accounts.canonical
    assert paths.private_codex.canonical == sidekick_root / "codex"
    assert (
        paths.private_codex.existing_sidekick == paths.private_codex.canonical
    )
    assert paths.lifetime_cache_file == (
        sidekick_root / "codex-lifetime-cache.json"
    )
    assert not home.exists()


def test_injected_account_locations_drive_prototype_import(
    tmp_path: Path,
) -> None:
    """The store imports only from its injected prototype location."""
    paths = make_application_paths(tmp_path)
    prototype_file = paths.accounts.prototype_cc_usage
    prototype_file.parent.mkdir(parents=True)
    prototype_content = json.dumps(
        {"team": {"token": "secret", "plan": "max"}}
    )
    prototype_file.write_text(prototype_content)

    account = AccountStore(paths.accounts).load().get("team")

    assert account is not None
    assert account.access_token == "secret"
    assert paths.accounts.canonical.exists()
    assert prototype_file.read_text() == prototype_content

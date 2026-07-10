"""Behavioral tests for Sidekick-owned path composition."""

import json
import os
from pathlib import Path

import pytest

from sidekick_usages.paths import discover_application_paths
from sidekick_usages.persistence.account_store import AccountStoreStateError
from sidekick_usages.persistence.errors import PersistenceCode
from tests.test_support import make_account_store, make_application_paths


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


def test_account_store_does_not_implicitly_import_prototype(
    tmp_path: Path,
) -> None:
    """Runtime loading leaves prototype migration to the coordinator."""
    paths = make_application_paths(tmp_path)
    prototype_file = paths.accounts.prototype_cc_usage
    prototype_file.parent.mkdir(parents=True)
    prototype_content = json.dumps(
        {"team": {"token": "secret", "plan": "max"}}
    )
    prototype_file.write_text(prototype_content)
    os.chmod(prototype_file.parent, 0o700)
    os.chmod(prototype_file, 0o600)

    with pytest.raises(AccountStoreStateError) as exc_info:
        make_account_store(tmp_path)

    assert exc_info.value.code is PersistenceCode.PROTOTYPE_IMPORT_REQUIRED
    assert not paths.accounts.canonical.exists()
    assert prototype_file.read_text() == prototype_content

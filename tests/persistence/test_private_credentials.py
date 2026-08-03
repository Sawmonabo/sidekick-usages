"""Foundational protected credential-tree tests."""

import os
import stat
from pathlib import Path

import pytest

from sidekick_usages.persistence.errors import (
    PrivateCredentialArtifactError,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.types.credential import (
    PrivateCredentialOwnership,
    PrivateCredentialState,
)
from tests.support.persistence import make_application_paths

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
RELEASED_DIRECTORY_MODE = 0o755


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _private_file(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.chmod(PRIVATE_FILE_MODE)


def test_tree_writes_reads_and_destroys_only_sidekick_credentials(
    tmp_path: Path,
) -> None:
    paths = make_application_paths(tmp_path / "sidekick")
    tree = PrivateCredentialTree(
        paths.private_credentials,
        account_path=paths.accounts,
    )
    external = tmp_path / "native-provider-auth.json"
    external.write_bytes(b"test-only-native-login")
    assert tree.observe() is PrivateCredentialState.ABSENT

    bundle = tree.write_bundle(
        tree.root / "primary",
        {
            "auth.json": b"test-only-sidekick-auth",
            "config.toml": b'credential_store = "file"\n',
        },
        expected_bundle_present=False,
        expected_files={"auth.json": None},
    )

    observed = tree.read_relative_bundle("primary")
    assert observed is not None
    assert observed["auth.json"].data == b"test-only-sidekick-auth"
    assert tree.observe() is PrivateCredentialState.PRESENT
    if os.name != "nt":
        assert _mode(tree.root) == PRIVATE_DIRECTORY_MODE
        assert _mode(bundle) == PRIVATE_DIRECTORY_MODE
        assert _mode(bundle / "auth.json") == PRIVATE_FILE_MODE

    tree.destroy_all()

    assert tree.observe() is PrivateCredentialState.ABSENT
    assert external.read_bytes() == b"test-only-native-login"


def test_bundle_paths_are_confined_to_the_current_private_root(
    tmp_path: Path,
) -> None:
    tree = PrivateCredentialTree(tmp_path / "credentials")
    canonical = tree.root / "primary"

    assert (
        tree.classify_bundle(canonical) is PrivateCredentialOwnership.CANONICAL
    )
    assert (
        tree.classify_bundle(tmp_path / "outside")
        is PrivateCredentialOwnership.EXTERNAL
    )
    assert tree.relative_bundle_path(canonical) == "primary"
    assert tree.canonical_bundle_path("primary") == canonical
    with pytest.raises(ValueError, match="safe basename"):
        tree.canonical_bundle_path("../outside")
    with pytest.raises(ValueError, match="reserved namespace"):
        tree.canonical_bundle_path(".credential-transaction")


@pytest.mark.skipif(os.name == "nt", reason="POSIX object contract")
def test_destroy_rejects_symlink_without_touching_its_target(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external-auth.json"
    _private_file(external, b"test-only-external-secret")
    root = tmp_path / "credentials"
    root.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    (root / "auth.json").symlink_to(external)

    with pytest.raises(PrivateCredentialArtifactError):
        PrivateCredentialTree(root).destroy_all()

    assert external.read_bytes() == b"test-only-external-secret"
    assert (root / "auth.json").is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
def test_permission_repair_preserves_credential_bytes(
    tmp_path: Path,
) -> None:
    app_root = tmp_path / "sidekick"
    app_root.mkdir(mode=RELEASED_DIRECTORY_MODE)
    app_root.chmod(RELEASED_DIRECTORY_MODE)
    accounts = app_root / "accounts.json"
    _private_file(accounts, b"test-only-account-authority")
    root = app_root / "credentials"
    root.mkdir(mode=RELEASED_DIRECTORY_MODE)
    root.chmod(RELEASED_DIRECTORY_MODE)
    bundle = root / "primary"
    bundle.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    auth = bundle / "auth.json"
    _private_file(auth, b"test-only-private-secret")
    tree = PrivateCredentialTree(root, account_path=accounts)

    result = tree.repair_permissions(locked_precondition=lambda: None)

    assert result.account_parent_repaired
    assert result.directories_repaired == 1
    assert result.artifacts_present
    assert _mode(app_root) == PRIVATE_DIRECTORY_MODE
    assert _mode(root) == PRIVATE_DIRECTORY_MODE
    assert accounts.read_bytes() == b"test-only-account-authority"
    assert auth.read_bytes() == b"test-only-private-secret"

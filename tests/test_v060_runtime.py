"""Offline pinned v0.6.0 reader verification tests."""

import json
import os
import subprocess
import sys
import zipfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.persistence.artifacts import (
    FileFingerprint,
    FileIdentity,
    FileSnapshot,
    sha256_digest,
)
from sidekick_usages.persistence.errors import PersistenceCode
from sidekick_usages.persistence.schemas import encode_generation_zero
from sidekick_usages.persistence.transforms import (
    accounts_to_version_one,
    version_one_to_v060,
)
from sidekick_usages.persistence.v060 import (
    ReleasedReaderVerificationError,
    ReleasedV060Verifier,
    RollbackOracleUnavailableError,
)

_REPOSITORY = Path(__file__).resolve().parents[1]
_BUNDLE = (
    _REPOSITORY
    / "src"
    / "sidekick_usages"
    / "persistence"
    / "_compat"
    / "v060-reader.zip"
)
_RELEASE_FILES = (
    "sidekick_usages/__init__.py",
    "sidekick_usages/errors.py",
    "sidekick_usages/store.py",
)


def _rollback_payload() -> bytes:
    accounts = (
        Account(
            label=AccountLabel("claude-测试"),
            credentials=ClaudeCredentials(
                access_token="test-only-claude-access",
                refresh_token="test-only-claude-refresh",
                scopes=("user:profile",),
            ),
            plan="团队",
        ),
        Account(
            label=AccountLabel("codex-plus-1"),
            credentials=CodexCredentials(
                access_token="test-only-codex-access",
                refresh_token="test-only-codex-refresh",
                account_id="acct_test_only",
                auth_home="/synthetic/codex/account",
                id_token="test-only-codex-id",
            ),
            plan="plus",
        ),
    )
    version_one = accounts_to_version_one(accounts)
    return encode_generation_zero(version_one_to_v060(version_one))


def _file_snapshot(path: Path, payload: bytes) -> FileSnapshot:
    metadata = path.stat()
    return FileSnapshot(
        FileFingerprint(
            FileIdentity(metadata.st_dev, metadata.st_ino),
            sha256_digest(payload),
            len(payload),
        ),
        metadata.st_nlink,
        payload,
    )


def test_bundle_is_deterministic_exact_pinned_source(tmp_path: Path) -> None:
    """The shipped oracle is reproducible from only the approved commit."""
    generated = tmp_path / "v060-reader.zip"
    result = subprocess.run(
        (
            sys.executable,
            str(_REPOSITORY / "packaging/build_v060_reader_bundle.py"),
            "--repository",
            str(_REPOSITORY),
            "--output",
            str(generated),
        ),
        cwd=_REPOSITORY,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0
    assert generated.read_bytes() == _BUNDLE.read_bytes()
    with zipfile.ZipFile(generated) as bundle:
        assert tuple(sorted(bundle.namelist())) == (
            "MANIFEST.json",
            *_RELEASE_FILES,
        )


def test_released_reader_verifies_exact_file_and_rejects_change(
    tmp_path: Path,
) -> None:
    """The isolated old reader observes the committed file, not a fixture."""
    payload = _rollback_payload()
    account_path = tmp_path / "accounts.json"
    account_path.write_bytes(payload)
    committed = _file_snapshot(account_path, payload)
    verifier = ReleasedV060Verifier()

    verifier.preflight()
    verifier.verify(account_path, committed)

    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(payload)
    os.replace(replacement, account_path)
    with pytest.raises(ReleasedReaderVerificationError):
        verifier.verify(account_path, committed)
    committed = _file_snapshot(account_path, payload)

    equivalent = json.dumps(
        json.loads(payload),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    account_path.write_bytes(equivalent)
    with pytest.raises(ReleasedReaderVerificationError) as exc_info:
        verifier.verify(account_path, committed)
    assert exc_info.value.code is PersistenceCode.DURABILITY_UNCERTAIN

    account_path.unlink()
    with pytest.raises(ReleasedReaderVerificationError):
        verifier.verify(account_path, committed)


class RejectingRunner:
    """Return a raw failing subprocess result without exposing its text."""

    def __call__(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        environment: Mapping[str, str],
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment
        return subprocess.CompletedProcess(
            argv,
            42,
            stdout="test-only-raw-secret",
            stderr="test-only-native-detail",
        )


def test_verifier_failures_are_phase_typed_and_secret_safe(
    tmp_path: Path,
) -> None:
    """Preflight and post-commit failures have distinct safe outcomes."""
    payload = _rollback_payload()
    account_path = tmp_path / "accounts.json"
    account_path.write_bytes(payload)
    expected = _file_snapshot(account_path, payload)
    verifier = ReleasedV060Verifier(runner=RejectingRunner())

    with pytest.raises(RollbackOracleUnavailableError) as preflight:
        verifier.preflight()
    with pytest.raises(ReleasedReaderVerificationError) as post_commit:
        verifier.verify(account_path, expected)

    assert preflight.value.code is PersistenceCode.ROLLBACK_REQUIRED
    assert post_commit.value.code is PersistenceCode.DURABILITY_UNCERTAIN
    representation = repr((preflight.value, post_commit.value))
    assert "raw-secret" not in representation
    assert "native-detail" not in representation

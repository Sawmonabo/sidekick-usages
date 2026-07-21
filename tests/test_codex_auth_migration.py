"""Behavioral tests for read-only Codex auth relocation preparation."""

import base64
import json
import os
from dataclasses import asdict
from pathlib import Path

import pytest

from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.persistence.migrations.ports import (
    PreparedPrivateAuthMigration,
    PrivateAuthBundleSnapshot,
    PrivateAuthHomeKind,
    PrivateAuthMigrationFailure,
    PrivateAuthMigrationFailureCode,
    PrivateAuthMigrationRequest,
    PrivateAuthPermission,
)
from sidekick_usages.providers.codex.auth import (
    CODEX_AUTH_FILE,
    CODEX_CONFIG_FILE,
    CODEX_FILE_AUTH_CONFIG,
)
from sidekick_usages.providers.codex.auth_migration import (
    CodexPrivateAuthMigrator,
)

_CONFIG = f"{CODEX_FILE_AUTH_CONFIG}\n".encode()


def _access_token(account_id: str) -> str:
    def encode(value: dict[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = encode({"alg": "none"})
    payload = encode(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
            }
        }
    )
    return f"{header}.{payload}.sig"


def _codex_account(label: str, home: Path, account_id: str) -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=CodexCredentials(
            access_token=_access_token(account_id),
            refresh_token=f"refresh-secret-{account_id}",
            account_id=account_id,
            auth_home=str(home),
        ),
    )


def _bundle(account_id: str) -> dict[str, bytes]:
    return {
        CODEX_AUTH_FILE: json.dumps(
            {
                "future_metadata": {"preserved": True},
                "tokens": {
                    "access_token": _access_token(account_id),
                    "account_id": account_id,
                },
            }
        ).encode(),
        CODEX_CONFIG_FILE: _CONFIG,
    }


def _snapshot(
    home: Path,
    files: dict[str, bytes] | None,
) -> PrivateAuthBundleSnapshot:
    return PrivateAuthBundleSnapshot(
        home=home,
        present=files is not None,
        files={} if files is None else files,
    )


def _request(
    accounts: tuple[Account, ...],
    source_root: Path,
    source_kind: PrivateAuthHomeKind,
    target_root: Path,
    target_kind: PrivateAuthHomeKind,
    *bundles: PrivateAuthBundleSnapshot,
) -> PrivateAuthMigrationRequest:
    return PrivateAuthMigrationRequest(
        accounts=accounts,
        source_root=source_root,
        source_kind=source_kind,
        target_root=target_root,
        target_kind=target_kind,
        bundles=bundles,
    )


def test_prepare_classifies_and_rewrites_exact_nested_bundles_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One batch preserves account order and sorts exact prepared copies."""
    compatibility = tmp_path / "compatibility" / "codex"
    canonical = tmp_path / "canonical" / "codex"
    zeta_home = compatibility / "teams" / "zeta"
    alpha_home = compatibility / "teams" / "alpha"
    canonical_home = canonical / "already"
    native_home = tmp_path / "provider-native"
    external_home = compatibility.with_name("codex-backup") / "external"
    for home in (zeta_home, alpha_home, canonical_home):
        home.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(native_home))

    accounts = (
        _codex_account("zeta", zeta_home, "acct_zeta"),
        _codex_account("alpha", alpha_home, "acct_alpha"),
        _codex_account("canonical", canonical_home, "acct_canonical"),
        _codex_account("native", native_home, "acct_native"),
        _codex_account("external", external_home, "acct_external"),
        Account(
            label=AccountLabel("claude"),
            credentials=ClaudeSetupTokenCredentials(
                access_token="claude-secret"
            ),
        ),
    )
    zeta_files = _bundle("acct_zeta")
    alpha_files = _bundle("acct_alpha")
    canonical_files = _bundle("acct_canonical")
    request = _request(
        accounts,
        compatibility,
        PrivateAuthHomeKind.COMPATIBILITY,
        canonical,
        PrivateAuthHomeKind.CANONICAL,
        _snapshot(zeta_home, zeta_files),
        _snapshot(canonical / "teams" / "zeta", None),
        _snapshot(alpha_home, alpha_files),
        _snapshot(canonical / "teams" / "alpha", None),
        _snapshot(canonical_home, canonical_files),
    )

    result = CodexPrivateAuthMigrator().prepare(request)

    assert isinstance(result, PreparedPrivateAuthMigration)
    assert [item.kind for item in result.assessment.accounts] == [
        PrivateAuthHomeKind.COMPATIBILITY,
        PrivateAuthHomeKind.COMPATIBILITY,
        PrivateAuthHomeKind.CANONICAL,
        PrivateAuthHomeKind.PROVIDER_NATIVE,
        PrivateAuthHomeKind.EXTERNAL,
        PrivateAuthHomeKind.UNSET,
    ]
    assert result.assessment.copies_required == len(result.copies)
    assert [copy.relative_path for copy in result.copies] == [
        "teams/alpha",
        "teams/zeta",
    ]
    assert all(
        copy.permission is PrivateAuthPermission.OWNER_ONLY
        for copy in result.copies
    )
    assert result.copies[0].bundle.files == alpha_files
    assert result.copies[1].bundle.files == zeta_files
    assert [str(account.label) for account in result.accounts] == [
        str(account.label) for account in accounts
    ]
    assert result.accounts[0].codex_home == str(canonical / "teams" / "zeta")
    assert result.accounts[1].codex_home == str(canonical / "teams" / "alpha")
    assert result.accounts[2].codex_home == str(canonical_home)
    assert result.accounts[3].codex_home == str(native_home)
    assert result.accounts[4].codex_home == str(external_home)
    assert accounts[0].codex_home == str(zeta_home)
    assert not (canonical / "teams" / "zeta").exists()
    assert not (canonical / "teams" / "alpha").exists()

    summary = json.dumps(asdict(result.assessment))
    assert str(tmp_path) not in summary
    assert "secret" not in summary
    assert "secret" not in repr(result)


def test_prepare_supports_canonical_to_compatibility_rollback(
    tmp_path: Path,
) -> None:
    """The same source/target contract prepares reverse exact-byte copies."""
    canonical = tmp_path / "canonical" / "codex"
    compatibility = tmp_path / "compatibility" / "codex"
    source = canonical / "nested" / "account"
    target = compatibility / "nested" / "account"
    source.mkdir(parents=True)
    account = _codex_account("rollback", source, "acct_rollback")
    files = _bundle("acct_rollback")

    result = CodexPrivateAuthMigrator().prepare(
        _request(
            (account,),
            canonical,
            PrivateAuthHomeKind.CANONICAL,
            compatibility,
            PrivateAuthHomeKind.COMPATIBILITY,
            _snapshot(source, files),
            _snapshot(target, None),
        )
    )

    assert isinstance(result, PreparedPrivateAuthMigration)
    assert result.accounts[0].codex_home == str(target)
    assert result.assessment.accounts[0].kind is PrivateAuthHomeKind.CANONICAL
    assert result.copies[0].bundle.files == files
    assert result.copies[0].relative_path == "nested/account"
    assert not target.exists()


@pytest.mark.parametrize("ending", [b"", b"\n", b"\r\n"])
def test_prepare_accepts_released_private_config_line_endings(
    tmp_path: Path,
    ending: bytes,
) -> None:
    """Released private configs migrate without rewriting exact bytes."""
    compatibility = tmp_path / "compatibility"
    canonical = tmp_path / "canonical"
    source = compatibility / "account"
    target = canonical / "account"
    source.mkdir(parents=True)
    account = _codex_account("account", source, "acct_released")
    files = _bundle("acct_released")
    files[CODEX_CONFIG_FILE] = CODEX_FILE_AUTH_CONFIG.encode() + ending

    result = CodexPrivateAuthMigrator().prepare(
        _request(
            (account,),
            compatibility,
            PrivateAuthHomeKind.COMPATIBILITY,
            canonical,
            PrivateAuthHomeKind.CANONICAL,
            _snapshot(source, files),
            _snapshot(target, None),
        )
    )

    assert isinstance(result, PreparedPrivateAuthMigration)
    assert result.copies[0].bundle.files == files


def test_semantic_digest_normalizes_roots_and_changes_with_exact_bytes(
    tmp_path: Path,
) -> None:
    """Equivalent owned bundles compare equally without exposing bytes."""
    compatibility = tmp_path / "compatibility"
    canonical = tmp_path / "canonical"
    old_home = compatibility / "nested" / "account"
    new_home = canonical / "nested" / "account"
    old_home.mkdir(parents=True)
    files = _bundle("acct_digest")
    old_result = CodexPrivateAuthMigrator().prepare(
        _request(
            (_codex_account("digest", old_home, "acct_digest"),),
            compatibility,
            PrivateAuthHomeKind.COMPATIBILITY,
            canonical,
            PrivateAuthHomeKind.CANONICAL,
            _snapshot(old_home, files),
            _snapshot(new_home, None),
        )
    )
    new_home.mkdir(parents=True)
    new_account = _codex_account("digest", new_home, "acct_digest")
    new_result = CodexPrivateAuthMigrator().prepare(
        _request(
            (new_account,),
            compatibility,
            PrivateAuthHomeKind.COMPATIBILITY,
            canonical,
            PrivateAuthHomeKind.CANONICAL,
            _snapshot(new_home, files),
        )
    )
    changed = dict(files)
    changed_blob = json.loads(changed[CODEX_AUTH_FILE])
    changed_blob["future_metadata"] = {"preserved": False}
    changed[CODEX_AUTH_FILE] = json.dumps(changed_blob).encode()
    changed_result = CodexPrivateAuthMigrator().prepare(
        _request(
            (new_account,),
            compatibility,
            PrivateAuthHomeKind.COMPATIBILITY,
            canonical,
            PrivateAuthHomeKind.CANONICAL,
            _snapshot(new_home, changed),
        )
    )

    assert isinstance(old_result, PreparedPrivateAuthMigration)
    assert isinstance(new_result, PreparedPrivateAuthMigration)
    assert isinstance(changed_result, PreparedPrivateAuthMigration)
    assert old_result.semantic_digest == new_result.semantic_digest
    assert changed_result.semantic_digest != new_result.semantic_digest
    assert str(old_result.semantic_digest) not in repr(old_result)


@pytest.mark.parametrize(
    ("source_files", "expected_code"),
    [
        (None, PrivateAuthMigrationFailureCode.SOURCE_MISSING),
        (
            {
                CODEX_AUTH_FILE: b"{malformed",
                CODEX_CONFIG_FILE: _CONFIG,
            },
            PrivateAuthMigrationFailureCode.SOURCE_INVALID,
        ),
        (
            {
                **_bundle("acct_expected"),
                CODEX_CONFIG_FILE: b"untrusted_setting = true\n",
            },
            PrivateAuthMigrationFailureCode.SOURCE_INVALID,
        ),
        (
            _bundle("acct_other"),
            PrivateAuthMigrationFailureCode.SOURCE_IDENTITY_MISMATCH,
        ),
    ],
)
def test_prepare_rejects_untrusted_source_bundles_without_secret_output(
    tmp_path: Path,
    source_files: dict[str, bytes] | None,
    expected_code: PrivateAuthMigrationFailureCode,
) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source = source_root / "account"
    target = target_root / "account"
    source.mkdir(parents=True)
    account = _codex_account("account", source, "acct_expected")

    result = CodexPrivateAuthMigrator().prepare(
        _request(
            (account,),
            source_root,
            PrivateAuthHomeKind.COMPATIBILITY,
            target_root,
            PrivateAuthHomeKind.CANONICAL,
            _snapshot(source, source_files),
            _snapshot(target, None),
        )
    )

    assert isinstance(result, PrivateAuthMigrationFailure)
    assert result.code is expected_code
    rendered = json.dumps(asdict(result))
    assert "secret" not in rendered
    assert str(tmp_path) not in rendered
    assert account.codex_home == str(source)
    assert not target.exists()


def test_failure_summary_is_bounded_and_deterministic() -> None:
    """Doctor-facing failures normalize labels and reject unsafe text."""
    failure = PrivateAuthMigrationFailure(
        code=PrivateAuthMigrationFailureCode.SOURCE_INVALID,
        message="A private Codex source is invalid.",
        accounts=(
            AccountLabel("zeta"),
            AccountLabel("alpha"),
            AccountLabel("zeta"),
        ),
    )

    assert failure.accounts == (AccountLabel("alpha"), AccountLabel("zeta"))
    assert set(asdict(failure)) == {"code", "message", "accounts"}
    for unsafe in ("", "line\nbreak", "x" * 1025):
        with pytest.raises(
            ValueError,
            match="Private-auth failure message",
        ):
            PrivateAuthMigrationFailure(
                code=PrivateAuthMigrationFailureCode.SOURCE_INVALID,
                message=unsafe,
            )


@pytest.mark.parametrize(
    ("target_files", "expected_code"),
    [
        (
            {CODEX_AUTH_FILE: _bundle("acct_expected")[CODEX_AUTH_FILE]},
            PrivateAuthMigrationFailureCode.TARGET_PARTIAL,
        ),
        (
            {
                **_bundle("acct_expected"),
                CODEX_CONFIG_FILE: b"conflicting-config",
            },
            PrivateAuthMigrationFailureCode.TARGET_CONFLICT,
        ),
    ],
)
def test_prepare_distinguishes_partial_and_conflicting_targets(
    tmp_path: Path,
    target_files: dict[str, bytes],
    expected_code: PrivateAuthMigrationFailureCode,
) -> None:
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source = source_root / "account"
    target = target_root / "account"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    account = _codex_account("account", source, "acct_expected")

    result = CodexPrivateAuthMigrator().prepare(
        _request(
            (account,),
            source_root,
            PrivateAuthHomeKind.COMPATIBILITY,
            target_root,
            PrivateAuthHomeKind.CANONICAL,
            _snapshot(source, _bundle("acct_expected")),
            _snapshot(target, target_files),
        )
    )

    assert isinstance(result, PrivateAuthMigrationFailure)
    assert result.code is expected_code
    assert account.codex_home == str(source)


def test_prepare_rejects_nested_target_aliases(
    tmp_path: Path,
) -> None:
    """Ancestor destinations cannot alias another account's bundle."""
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    parent = source_root / "group"
    child = parent / "account"
    child.mkdir(parents=True)
    accounts = (
        _codex_account("parent", parent, "acct_parent"),
        _codex_account("child", child, "acct_child"),
    )

    result = CodexPrivateAuthMigrator().prepare(
        _request(
            accounts,
            source_root,
            PrivateAuthHomeKind.COMPATIBILITY,
            target_root,
            PrivateAuthHomeKind.CANONICAL,
            _snapshot(parent, _bundle("acct_parent")),
            _snapshot(target_root / "group", None),
            _snapshot(child, _bundle("acct_child")),
            _snapshot(target_root / "group" / "account", None),
        )
    )

    assert isinstance(result, PrivateAuthMigrationFailure)
    assert result.code is PrivateAuthMigrationFailureCode.TARGET_COLLISION
    assert result.accounts == (AccountLabel("child"), AccountLabel("parent"))


@pytest.mark.skipif(os.name == "nt", reason="Windows reparse proof is native")
def test_prepare_rejects_symlink_escape(
    tmp_path: Path,
) -> None:
    """Lexical descendants must also remain below the resolved source root."""
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    outside = tmp_path / "outside"
    source_root.mkdir()
    outside.mkdir()
    escaped = source_root / "escaped"
    escaped.symlink_to(outside, target_is_directory=True)
    account = _codex_account("escaped", escaped, "acct_escape")

    result = CodexPrivateAuthMigrator().prepare(
        _request(
            (account,),
            source_root,
            PrivateAuthHomeKind.COMPATIBILITY,
            target_root,
            PrivateAuthHomeKind.CANONICAL,
            _snapshot(escaped, _bundle("acct_escape")),
            _snapshot(target_root / "escaped", None),
        )
    )

    assert isinstance(result, PrivateAuthMigrationFailure)
    assert result.code is PrivateAuthMigrationFailureCode.UNSAFE_HOME
    assert not (target_root / "escaped").exists()

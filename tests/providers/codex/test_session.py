"""Load-bearing tests for the neutral Codex session boundary."""

import os
import stat
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest

from sidekick_usages.core.accounts.types import (
    AuthorityId,
    SidekickAccountId,
)
from sidekick_usages.paths import (
    ApplicationPaths,
    discover_application_paths,
    managed_codex_home,
)
from sidekick_usages.persistence.accounts.reader import AccountIndexReader
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    claude_credential_basename,
)
from sidekick_usages.providers.codex.auth.home import default_codex_home
from sidekick_usages.providers.codex.auth.storage import codex_auth_basename
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.service import (
    prepare_codex_session_home,
)
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from sidekick_usages.providers.codex.session.home import (
    CodexSessionAccountReader,
)
from sidekick_usages.providers.codex.session.models import (
    CODEX_SESSION_BASE_URL,
    CODEX_SESSION_MODEL_PROVIDER,
    CODEX_SESSION_OPERATOR_PRECONDITION,
    CodexSessionConfigurationReason,
)
from tests.fakes.codex.auth import managed_auth
from tests.fakes.codex.managed import (
    managed_saved_account,
    seed_managed_accounts,
)
from tests.support.persistence import make_application_paths
from tests.support.platform import REQUIRES_MANAGED_RUNTIME

pytestmark = REQUIRES_MANAGED_RUNTIME

_ACCOUNT_ID = SidekickAccountId("11111111-1111-4111-8111-111111111111")
_AUTHORITY_ID = AuthorityId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_PROVIDER_IDENTITY = "workspace-account-alpha"
_GENERATION = "2026-07-24T10:00:00.000000000Z"
_PRIVATE_DIRECTORY_MODE = 0o700


def _prepare(
    paths: ApplicationPaths,
    *,
    account_reader: CodexSessionAccountReader | None = None,
    native_home: Path | None = None,
) -> Path:
    """Compose the production session owner with qualified persistence."""
    return prepare_codex_session_home(
        paths,
        lambda root: PrivateCredentialTree(
            root,
            account_path=paths.accounts,
        ),
        (
            AccountIndexReader(paths.accounts).load
            if account_reader is None
            else account_reader
        ),
        native_home=(
            default_codex_home() if native_home is None else native_home
        ),
        forbidden_entries=(
            codex_auth_basename(),
            claude_credential_basename(),
        ),
    )


def test_session_home_is_created_only_by_its_qualified_owner(
    tmp_path: Path,
) -> None:
    paths = make_application_paths(tmp_path / "state")
    assert not paths.codex_session_home.exists()

    prepared = _prepare(paths)

    assert prepared == paths.codex_session_home
    metadata = prepared.lstat()
    assert stat.S_ISDIR(metadata.st_mode)
    assert metadata.st_uid == os.geteuid()
    assert stat.S_IMODE(metadata.st_mode) == _PRIVATE_DIRECTORY_MODE
    assert prepared.resolve(strict=True) == prepared
    assert {entry.name for entry in prepared.iterdir()} == {
        "config.toml",
        "packages",
    }


def test_session_home_prepares_official_daemon_without_credentials(
    tmp_path: Path,
) -> None:
    paths = make_application_paths(tmp_path / "state")
    paths.codex_session_home.parent.mkdir(parents=True, mode=0o700)
    paths.codex_session_home.parent.chmod(0o700)
    paths.codex_session_home.mkdir(mode=0o700)
    settings = paths.codex_session_home / "config.toml"
    expected = (
        b'analytics = false\nmodel = "synthetic"\n'
        b'[model_providers.synthetic]\nname = "Synthetic"\n'
        b"[features]\nresponses_websockets = true\n"
    )
    settings.write_bytes(expected)
    settings.chmod(0o600)
    native_home = tmp_path / "native-codex"
    managed = native_home.joinpath(
        "packages",
        "standalone",
        "current",
        "codex",
    )
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"synthetic executable")

    assert _prepare(
        paths,
        account_reader=lambda: (),
        native_home=native_home,
    ) == (paths.codex_session_home)
    projected = paths.codex_session_home / "packages"
    assert projected.is_symlink()
    assert projected.resolve(strict=True) == native_home / "packages"
    payload = settings.read_bytes()
    assert expected in payload
    document = tomllib.loads(payload.decode())
    assert document["features"]["responses_websockets"] is True
    assert document["model_providers"]["synthetic"]["name"] == "Synthetic"
    assert document["model_provider"] == CODEX_SESSION_MODEL_PROVIDER
    provider = document["model_providers"][CODEX_SESSION_MODEL_PROVIDER]
    assert provider["base_url"] == CODEX_SESSION_BASE_URL
    assert provider["requires_openai_auth"] is True
    assert provider["supports_websockets"] is False
    assert not (paths.codex_session_home / "auth.json").exists()


@pytest.mark.parametrize(
    ("hazard", "reason"),
    [
        pytest.param(
            "redirect",
            CodexSessionConfigurationReason.HOME_UNSAFE,
            id="redirect",
        ),
        pytest.param(
            "mode",
            CodexSessionConfigurationReason.HOME_UNSAFE,
            id="wrong-mode",
        ),
        pytest.param(
            "owner",
            CodexSessionConfigurationReason.HOME_UNSAFE,
            id="wrong-owner",
        ),
        pytest.param(
            "auth.json",
            CodexSessionConfigurationReason.CREDENTIAL_STATE_PRESENT,
            id="auth-json",
        ),
        pytest.param(
            ".credentials.json",
            CodexSessionConfigurationReason.CREDENTIAL_STATE_PRESENT,
            id="credentials-json",
        ),
    ],
)
def test_session_home_rejects_unsafe_or_credential_bearing_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hazard: str,
    reason: CodexSessionConfigurationReason,
) -> None:
    paths = make_application_paths(tmp_path / "state")
    home = paths.codex_session_home
    if hazard == "redirect":
        redirected = tmp_path / "redirected"
        redirected.mkdir(mode=0o700)
        home.parent.parent.mkdir(parents=True, mode=0o700)
        home.parent.symlink_to(redirected, target_is_directory=True)
    else:
        home.parent.mkdir(parents=True, mode=0o700)
        home.parent.chmod(0o700)
        home.mkdir(mode=0o700)
        if hazard == "mode":
            home.chmod(0o750)
        elif hazard == "owner":
            actual_user_id = os.geteuid()
            monkeypatch.setattr(
                "sidekick_usages.providers.codex.session.home."
                "_current_user_id",
                lambda: actual_user_id + 1,
            )
        else:
            credential = home / hazard
            credential.write_bytes(b"synthetic-credential-state")
            credential.chmod(0o600)

    with pytest.raises(CodexBrokerError) as refused:
        _prepare(paths, account_reader=lambda: ())

    assert refused.value.code is (
        CodexBrokerFailure.SESSION_CONFIGURATION_REQUIRED
    )
    report = refused.value.preparation_report
    assert report is not None
    assert report.reason is reason
    assert report.dry_run is True
    assert report.operator_steps[0] == CODEX_SESSION_OPERATOR_PRECONDITION


@pytest.mark.parametrize(
    "collision",
    ["saved-home", "private-root", "inside-root", "contains-root"],
)
def test_session_home_cannot_overlap_a_saved_private_authority(
    tmp_path: Path,
    collision: str,
) -> None:
    account = managed_saved_account(
        _ACCOUNT_ID,
        _AUTHORITY_ID,
        "codex-alpha",
        _PROVIDER_IDENTITY,
        _GENERATION,
    )
    paths, _store, _private = seed_managed_accounts(
        tmp_path / "state",
        (account,),
        {
            _ACCOUNT_ID: managed_auth(
                _PROVIDER_IDENTITY,
                _GENERATION,
            )
        },
    )
    private_home = managed_codex_home(paths, _ACCOUNT_ID)
    session_home = {
        "saved-home": private_home,
        "private-root": paths.private_codex_profiles,
        "inside-root": paths.private_codex_profiles / "neutral",
        "contains-root": paths.private_codex_profiles.parent,
    }[collision]
    colliding = replace(paths, codex_session_home=session_home)

    with pytest.raises(CodexBrokerError) as refused:
        _prepare(colliding)

    report = refused.value.preparation_report
    assert report is not None
    assert report.operator_steps[0] == CODEX_SESSION_OPERATOR_PRECONDITION
    assert (
        report.reason
        is CodexSessionConfigurationReason.PRIVATE_AUTHORITY_COLLISION
    )


@pytest.mark.parametrize(
    "authority_source",
    ["codex-home", "codex-home-child", "xdg-home"],
)
def test_session_home_cannot_overlap_native_codex_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authority_source: str,
) -> None:
    if authority_source.startswith("codex-home"):
        paths = make_application_paths(tmp_path / "state")
        native_home = paths.codex_session_home
        if authority_source == "codex-home-child":
            native_home /= "native-authority"
        monkeypatch.setenv("CODEX_HOME", str(native_home))
    else:
        native_home = tmp_path / "native-home"
        monkeypatch.setenv("HOME", str(native_home))
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(native_home / ".codex"))
        paths = discover_application_paths()

    with pytest.raises(CodexBrokerError) as refused:
        _prepare(paths)

    report = refused.value.preparation_report
    assert report is not None
    assert (
        report.reason
        is CodexSessionConfigurationReason.PRIVATE_AUTHORITY_COLLISION
    )

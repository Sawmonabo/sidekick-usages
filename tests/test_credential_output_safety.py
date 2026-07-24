"""Credential export and diagnostic output-safety tests."""

import io
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest
from rich.console import Console

import sidekick_usages.credentials.codex
import sidekick_usages.providers.claude.provider
from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import Account, ClaudeLoginCredentials
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials import CredentialService
from sidekick_usages.credentials.authorities import credential_resolver_for
from sidekick_usages.credentials.refresh import CredentialRefreshCoordinator
from sidekick_usages.doctor import (
    DoctorReadyResult,
    DoctorService,
    doctor_json,
    render_doctor,
)
from sidekick_usages.errors import AuthError
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.maintenance import TokenMaintenanceService
from sidekick_usages.persistence.credential_refresh import (
    CredentialRefreshState,
    CredentialRefreshStateKind,
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.errors import ReplaceFailedError
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.models.status import PersistenceStatus
from sidekick_usages.persistence.types.status import PersistenceState
from sidekick_usages.providers.base import ProviderFailure, ProviderFailureKind
from sidekick_usages.providers.claude.provider import ClaudeProvider
from sidekick_usages.serialization import JsonObject
from tests.test_credential_service import (
    _PRIVATE_DIRECTORY_MODE,
    _PRIVATE_FILE_MODE,
    _account,
    _dependencies,
    _Provider,
    _service,
)
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_application_paths,
    make_supervisor_health,
)


def test_export_protects_paths_and_publishes_auth_authority_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account("acct-new")
    provider = _Provider(
        ProviderId.CODEX,
        ProviderFailure(
            provider_id=ProviderId.CODEX,
            kind=ProviderFailureKind.MISSING,
            message="No local credentials.",
        ),
    )
    service, _, private = _service(tmp_path, provider, (account,))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-active"))

    protected = service.export_codex("team", private.root / "nested")

    assert isinstance(protected, ProviderFailure)
    assert protected.kind is ProviderFailureKind.UNSUPPORTED

    target = tmp_path / "exported"
    calls: list[str] = []
    original = PersistenceFilesystem.commit_opaque_private

    def fail_auth(
        filesystem: PersistenceFilesystem,
        payload: bytes,
        *,
        expected_source: ExpectedAuthority | None = None,
    ) -> FileSnapshot:
        calls.append(filesystem.authority_path.name)
        if filesystem.authority_path.name == "auth.json":
            raise ReplaceFailedError
        return original(
            filesystem,
            payload,
            expected_source=expected_source,
        )

    monkeypatch.setattr(
        sidekick_usages.credentials.codex.PersistenceFilesystem,
        "commit_opaque_private",
        fail_auth,
    )
    failed = service.export_codex("team", target)

    assert isinstance(failed, ProviderFailure)
    assert failed.kind is ProviderFailureKind.UNREADABLE
    assert calls == ["config.toml", "auth.json"], failed
    assert (target / "config.toml").is_file()
    assert not (target / "auth.json").exists()
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == _PRIVATE_DIRECTORY_MODE
        assert (
            stat.S_IMODE((target / "config.toml").stat().st_mode)
            == _PRIVATE_FILE_MODE
        )
    PersistenceFilesystem(target / "config.toml").read_opaque_private()


def test_provider_secret_never_crosses_persisted_or_doctor_error_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One provider rejection remains secret-safe through every consumer."""
    response_secret = "test-only-provider-response-secret"
    account = Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-saved-access",
            refresh_token="test-only-saved-refresh",
            access_expiry=KnownExpiry(REFERENCE_TIME),
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile",),
        ),
        plan="team",
    )
    store, private = _dependencies(tmp_path, (account,))
    clock = FixedClock()
    provider = ClaudeProvider(clock)
    http = HttpClient(clock=clock)
    refresh = CredentialRefreshCoordinator(
        store,
        http,
        {ProviderId.CLAUDE: provider},
        CredentialRefreshTransactions(
            store,
            make_application_paths(tmp_path).credential_refresh,
        ),
        clock=clock,
        resolver=credential_resolver_for(store, private),
    )
    service = CredentialService(
        store,
        http,
        {ProviderId.CLAUDE: provider},
        private,
        clock=clock,
        refresh_coordinator=refresh,
    )
    monkeypatch.setattr(
        sidekick_usages.providers.claude.provider.shutil,
        "which",
        lambda _name: None,
    )

    def reject_refresh(
        url: str,
        json_body: JsonObject,
        headers: Mapping[str, str] | None = None,
        *,
        operation: HttpOperation,
    ) -> JsonObject:
        del url, json_body, headers, operation
        raise AuthError(response_secret)

    monkeypatch.setattr(http, "post_json", reject_refresh)

    outcome = TokenMaintenanceService(
        store,
        service,
        clock=clock,
    ).refresh_account(account, force=True)
    saved = store.get("team")
    assert saved is not None
    diagnostics = DoctorService(
        tuple(store),
        {ProviderId.CLAUDE: provider},
        {},
        clock,
    ).diagnostics()
    completed = DoctorReadyResult(
        tuple(diagnostics),
        PersistenceStatus(
            PersistenceState.CURRENT,
            store.path,
            1,
        ),
        CredentialRefreshState(CredentialRefreshStateKind.CLEAN),
        make_supervisor_health(),
    )
    human_output = io.StringIO()
    Console(file=human_output, force_terminal=False).print(
        render_doctor(completed, width=80)
    )
    machine_output = json.dumps(doctor_json(completed))

    assert saved.last_refresh_error == (
        "Claude rejected the saved subscription login."
    )
    for rendered in (
        repr(outcome),
        store.path.read_text(),
        repr(diagnostics),
        human_output.getvalue(),
        machine_output,
    ):
        assert response_secret not in rendered

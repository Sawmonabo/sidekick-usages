"""Credential diagnostic output-safety test."""

import io
import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from rich.console import Console

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import Account, ClaudeLoginCredentials
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.authorities import credential_resolver_for
from sidekick_usages.credentials.refresh import CredentialRefreshCoordinator
from sidekick_usages.credentials.service import CredentialService
from sidekick_usages.doctor.accounts.models import DoctorReadyResult
from sidekick_usages.doctor.accounts.service import DoctorService
from sidekick_usages.doctor.presentation.service import (
    doctor_json,
    render_doctor,
)
from sidekick_usages.errors import AuthError
from sidekick_usages.http.client import HttpClient
from sidekick_usages.http.types import HttpOperation
from sidekick_usages.maintenance import TokenMaintenanceService
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.credentials.refresh.artifacts import (
    CredentialRefreshState,
    CredentialRefreshStateKind,
)
from sidekick_usages.persistence.credentials.refresh.service import (
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.models.status import PersistenceStatus
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.types.status import PersistenceState
from sidekick_usages.providers.claude.provider import ClaudeProvider
from sidekick_usages.serialization.json import JsonObject
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_application_paths,
    make_supervisor_health,
)


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
    paths = make_application_paths(tmp_path)
    PersistenceFilesystem(paths.accounts).repair_parent_permissions()
    private = PrivateCredentialTree(
        paths.private_credentials,
        account_path=paths.accounts,
    )
    store = AccountStore(paths.accounts, private).load()
    store.persist(account)
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
        refresh_coordinator=refresh,
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
    ).refresh_account(store.saved_accounts()[0], force=True)
    saved = store.saved_accounts()[0]
    diagnostics = DoctorService(
        store.saved_accounts(),
        {ProviderId.CLAUDE},
        set(),
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

    for rendered in (
        repr(outcome),
        repr(saved),
        store.path.read_text(),
        repr(diagnostics),
        human_output.getvalue(),
        machine_output,
    ):
        assert response_secret not in rendered

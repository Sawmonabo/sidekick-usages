"""Credential diagnostic output-safety test."""

import io
import json
from pathlib import Path

from rich.console import Console

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import Account, ClaudeLoginCredentials
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.authorities import credential_resolver_for
from sidekick_usages.credentials.refresh import CredentialRefreshCoordinator
from sidekick_usages.credentials.service import CredentialService
from sidekick_usages.doctor.accounts.models import DoctorReadyResult
from sidekick_usages.doctor.accounts.service import DoctorService
from sidekick_usages.doctor.presentation.json import doctor_json
from sidekick_usages.doctor.presentation.service import render_doctor
from sidekick_usages.doctor.runtime.service import DoctorRuntimeService
from sidekick_usages.http.client import HttpClient
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
from tests.fakes.daemon.capabilities import (
    StaticProviderCapabilityService,
    make_provider_capability_report,
)
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_application_paths,
    make_supervisor_health,
)


def test_provider_secret_never_crosses_persisted_or_doctor_error_channels(
    tmp_path: Path,
) -> None:
    """Saved credentials remain secret-safe through every consumer."""
    access_secret = "sk-ant-oat01-saved-access"
    refresh_secret = "test-only-saved-refresh"
    account = Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token=access_secret,
            refresh_token=refresh_secret,
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

    outcome = TokenMaintenanceService(
        store,
        service,
        clock=clock,
    ).refresh_account(store.saved_accounts()[0], force=True)
    saved = store.saved_accounts()[0]
    capabilities = make_provider_capability_report()
    capability_service = StaticProviderCapabilityService(capabilities)
    diagnostics = DoctorService(
        store.saved_accounts(),
        capability_service,
        set(),
        clock,
        DoctorRuntimeService(store.saved_accounts(), None, (), (), ()),
    ).diagnostics()
    completed = DoctorReadyResult(
        diagnostics=tuple(diagnostics),
        scheduled_operations=(),
        unfinished_activations=(),
        persistence=PersistenceStatus(
            PersistenceState.CURRENT,
            store.path,
            1,
        ),
        refresh_state=CredentialRefreshState(
            CredentialRefreshStateKind.CLEAN
        ),
        supervisor=make_supervisor_health(),
        capabilities=capabilities,
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
        assert access_secret not in rendered
        assert refresh_secret not in rendered

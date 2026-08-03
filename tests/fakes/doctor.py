"""Doctor command test support."""

import io
from pathlib import Path

from rich.console import Console

from sidekick_usages.cli.contexts.models import DoctorContext, DoctorReady
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.credentials.capabilities.models import (
    ProviderCapabilityReport,
)
from sidekick_usages.daemon.models.lifecycle import SupervisorHealth
from sidekick_usages.daemon.types.lifecycle import ServiceComponentState
from sidekick_usages.doctor.accounts.service import DoctorService
from sidekick_usages.doctor.runtime.service import DoctorRuntimeService
from sidekick_usages.persistence.credentials.refresh.artifacts import (
    CredentialRefreshState,
    CredentialRefreshStateKind,
)
from sidekick_usages.persistence.lookup.store import (
    MetricsRefreshObservationStore,
)
from sidekick_usages.persistence.models.status import PersistenceStatus
from sidekick_usages.persistence.types.status import PersistenceState
from sidekick_usages.providers.registry import (
    build_heartbeat_registry,
    build_provider_registry,
)
from tests.fakes.daemon.capabilities import (
    StaticProviderCapabilityService,
    make_provider_capability_report,
)
from tests.support.cli import CliHarness
from tests.support.daemon import make_supervisor_health
from tests.support.persistence import make_application_paths
from tests.support.time import FixedClock


def doctor_harness(
    tmp_path: Path,
    accounts: tuple[SavedAccount, ...],
    runtime: DoctorRuntimeService | None = None,
    capabilities: ProviderCapabilityReport | None = None,
    supervisor: SupervisorHealth | None = None,
    refresh_state: CredentialRefreshStateKind = (
        CredentialRefreshStateKind.CLEAN
    ),
) -> tuple[CliHarness, io.StringIO, FixedClock]:
    """Build a Doctor CLI harness around deterministic account state."""
    output = io.StringIO()
    clock = FixedClock()
    providers = build_provider_registry(clock)
    heartbeat_providers = build_heartbeat_registry(providers)
    capability_report = (
        make_provider_capability_report()
        if capabilities is None
        else capabilities
    )
    capability_service = StaticProviderCapabilityService(capability_report)
    active_supervisor = (
        make_supervisor_health(queue=ServiceComponentState.UNHEALTHY)
        if supervisor is None
        else supervisor
    )
    refresh_diagnostic = MetricsRefreshObservationStore(
        make_application_paths(tmp_path).metrics_refresh_status
    ).diagnostic()
    active_runtime = (
        DoctorRuntimeService(accounts, None, (), (), ())
        if runtime is None
        else runtime
    )
    context = DoctorContext(
        DoctorReady(
            DoctorService(
                accounts,
                capability_service,
                heartbeat_providers.keys(),
                clock,
                active_runtime,
            ),
            PersistenceStatus(
                PersistenceState.CURRENT,
                tmp_path / "accounts.json",
                len(accounts),
            ),
            CredentialRefreshState(refresh_state),
            active_runtime.interactive,
        ),
        active_supervisor,
        capability_service,
        refresh_diagnostic,
    )
    return (
        CliHarness(
            console=Console(file=output, force_terminal=False, width=160),
            err_console=Console(
                file=io.StringIO(),
                force_terminal=False,
            ),
            doctor=context,
        ),
        output,
        clock,
    )

"""Synthetic dashboard snapshots, lookups, and invalidation."""

from collections.abc import Callable
from dataclasses import replace
from threading import Event
from time import monotonic

from sidekick_usages.cli.dashboard.session import InteractiveDashboardSession
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.types.service import ServicePhase
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardService,
    DashboardSnapshot,
    DashboardStatusKind,
)
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupEventKind,
    UsageLookupEventObserver,
    UsageLookupFailure,
    UsageLookupWorkerEvent,
    UsageLookupWorkerResult,
)
from tests.fakes.dashboard.session.models import SESSION_WAIT_SECONDS


class SessionSnapshotSource:
    """Return mutable synthetic cached truth through immutable snapshots."""

    def __init__(self, snapshot: DashboardSnapshot) -> None:
        self.snapshot = snapshot
        self.loads = 0

    def load(self, only: ProviderId | None) -> DashboardSnapshot:
        """Return the latest synthetic provider-proven state."""
        del only
        self.loads += 1
        return self.snapshot

    def activate(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
    ) -> None:
        """Publish one provider-verified active account."""
        providers = tuple(
            (
                replace(provider, actions_enabled=True)
                if provider.provider_id is not provider_id
                else replace(
                    provider,
                    runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
                    active_account_id=account_id,
                    actions_enabled=True,
                    rows=tuple(
                        (
                            replace(
                                row,
                                active=row.account_id == account_id,
                            )
                            if isinstance(row, DashboardAccount)
                            else row
                        )
                        for row in provider.rows
                    ),
                )
            )
            for provider in self.snapshot.providers
        )
        self.snapshot = replace(
            self.snapshot,
            providers=providers,
            service=DashboardService(
                ready=True,
                compatible=True,
                phase=ServicePhase.READY,
                observed_at=self.snapshot.reference_time,
                failure_code=None,
            ),
        )


class SessionLookupWorker:
    """Complete one stable lookup wave and record cancellation."""

    def __init__(
        self,
        account_id: SidekickAccountId,
        *,
        block: bool = False,
        fail: bool = False,
        transient_failure: UsageLookupFailure | None = None,
    ) -> None:
        self._account_id = account_id
        self._block = block
        self._fail = fail
        self._transient_failure = transient_failure
        self._release = Event()
        self.finished = Event()
        self.cancelled = False
        self.runs = 0

    def run(
        self,
        observe: UsageLookupEventObserver | None = None,
    ) -> UsageLookupWorkerResult:
        """Publish one stable-ID completion without provider work."""
        transient_failure = self._transient_failure
        self._transient_failure = None
        self.runs += 1
        try:
            if transient_failure is not None:
                return UsageLookupWorkerResult((), transient_failure)
            if self._block and not self._release.wait(SESSION_WAIT_SECONDS):
                raise AssertionError("Synthetic lookup was not released.")
            if observe is not None:
                observe(
                    UsageLookupWorkerEvent(
                        (
                            UsageLookupEventKind.ACCOUNT_FAILED
                            if self._fail
                            else UsageLookupEventKind.ACCOUNT_SUCCEEDED
                        ),
                        account_id=self._account_id,
                    )
                )
            if self._fail:
                raise OSError("Synthetic lookup owner failed.")
            return UsageLookupWorkerResult((self._account_id,))
        finally:
            if transient_failure is None:
                self.finished.set()

    def cancel(self) -> None:
        """Record one idempotent session cleanup request."""
        self.cancelled = True
        self._release.set()

    def release(self) -> None:
        """Release one synthetic blocked lookup without cancelling it."""
        self._release.set()

    def wait_until_finished(self) -> None:
        """Wait for one bounded owner completion."""
        if not self.finished.wait(SESSION_WAIT_SECONDS):
            raise AssertionError("Synthetic lookup owner did not finish.")


class SessionInvalidationProbe:
    """Wait for background invalidations without polling or sleeping."""

    def __init__(self) -> None:
        self._event = Event()
        self._session: InteractiveDashboardSession | None = None
        self.progress_messages: list[str] = []
        self.count = 0

    def bind_session(self, session: InteractiveDashboardSession) -> None:
        """Observe public footer state after each session invalidation."""
        self._session = session

    def __call__(self) -> None:
        """Record one thread-safe redraw request."""
        self.count += 1
        if self._session is not None:
            status = self._session.view.footer.status
            if (
                status is not None
                and status.kind is DashboardStatusKind.PROGRESS
            ):
                self.progress_messages.append(status.message)
        self._event.set()

    def wait_for(self, condition: Callable[[], bool]) -> None:
        """Wait for one bounded deterministic session transition."""
        deadline = monotonic() + SESSION_WAIT_SECONDS
        while not condition():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise AssertionError("Dashboard session did not advance.")
            self._event.clear()
            self._event.wait(remaining)


def unavailable_session_snapshot(
    snapshot: DashboardSnapshot,
) -> DashboardSnapshot:
    """Make one controller snapshot require guided service setup."""
    return replace(
        snapshot,
        providers=tuple(
            replace(provider, actions_enabled=False)
            for provider in snapshot.providers
        ),
        service=DashboardService(
            ready=False,
            compatible=False,
            phase=None,
            observed_at=None,
            failure_code=None,
        ),
    )

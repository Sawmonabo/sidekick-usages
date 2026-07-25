"""Exact-account services for isolated managed Codex workers."""

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.managed.resolver import (
    CodexManagedCredentialResolver,
)
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.heartbeat.models import HeartbeatOutcome
from sidekick_usages.heartbeat.service import HeartbeatService
from sidekick_usages.http.client import HttpClient
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.snapshots.activity import (
    ActivitySnapshotStore,
)
from sidekick_usages.persistence.snapshots.usage import UsageSnapshotStore
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthority,
)
from sidekick_usages.providers.codex.activity import CodexActivity
from sidekick_usages.providers.codex.heartbeat import CodexHeartbeat
from sidekick_usages.providers.codex.provider import CodexProvider
from sidekick_usages.usage.models import UsageCheckResult
from sidekick_usages.usage.ports import UsagePersistence
from sidekick_usages.usage.service import UsageCheckService


class CodexManagedAccountService:
    """Run account-scoped Codex services under an existing authority."""

    def __init__(
        self,
        coordinator: CodexManagedAuthorityCoordinator,
        store: AccountStore,
        http: HttpClient,
        activity_snapshots: ActivitySnapshotStore,
        usage_snapshots: UsageSnapshotStore,
        clock: Clock,
    ) -> None:
        self._coordinator = coordinator
        self._store = store
        self._http = http
        self._activity_snapshots = activity_snapshots
        self._usage_snapshots = usage_snapshots
        self._clock = clock

    def collect_metrics(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
    ) -> UsageCheckResult:
        """Collect current usage and activity for one exact account."""
        resolver = CodexManagedCredentialResolver(
            self._coordinator,
            authority,
        )
        service = UsageCheckService(
            self._store,
            self._http,
            {ProviderId.CODEX: CodexProvider(self._clock)},
            None,
            clock=self._clock,
            account_activity_sources={
                ProviderId.CODEX: CodexActivity(),
            },
            persistence=UsagePersistence(
                activity=self._activity_snapshots,
                usage=self._usage_snapshots,
            ),
            resolver=resolver,
        )
        return service.check_account(account_id)

    def heartbeat(
        self,
        account_id: SidekickAccountId,
        authority: OperationAuthority,
    ) -> tuple[HeartbeatOutcome, ...]:
        """Heartbeat enabled targets for one exact managed account."""
        resolver = CodexManagedCredentialResolver(
            self._coordinator,
            authority,
        )
        provider = CodexProvider(self._clock)
        service = HeartbeatService(
            self._store,
            self._http,
            {ProviderId.CODEX: CodexHeartbeat(provider)},
            clock=self._clock,
            resolver=resolver,
        )
        return service.heartbeat_saved_account(account_id)

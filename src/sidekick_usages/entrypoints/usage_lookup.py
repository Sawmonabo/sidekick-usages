"""Provider-heavy global usage lookup process entry point."""

import os
import sys
from collections.abc import Mapping, Sequence
from contextlib import ExitStack, suppress

from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.authorities import (
    credential_resolver_for,
)
from sidekick_usages.credentials.managed.composition import (
    compose_managed_credential_factories,
)
from sidekick_usages.credentials.refresh import CredentialRefreshCoordinator
from sidekick_usages.credentials.service import CredentialService
from sidekick_usages.http.client import HttpClient
from sidekick_usages.paths import ApplicationPaths, discover_application_paths
from sidekick_usages.persistence.credentials.refresh.service import (
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.service import PersistenceService
from sidekick_usages.persistence.snapshots.activity.store import (
    ActivitySnapshotStore,
)
from sidekick_usages.persistence.snapshots.usage.store import (
    UsageSnapshotStore,
)
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLocks,
)
from sidekick_usages.providers.claude.activity import (
    ClaudeActivity,
    discover_claude_config_dir,
)
from sidekick_usages.providers.codex.activity import CodexActivity
from sidekick_usages.providers.registry import build_provider_registry
from sidekick_usages.serialization.framing import clear_mutable_buffer
from sidekick_usages.usage.lookup.models import AccountLookupCompletion
from sidekick_usages.usage.lookup.service import AccountCredentialAccess
from sidekick_usages.usage.lookup.worker.models import (
    UsageLookupEventKind,
    UsageLookupFailure,
    UsageLookupTerminalFailure,
    UsageLookupWorkerEvent,
)
from sidekick_usages.usage.lookup.worker.protocol import (
    encode_usage_lookup_event,
)
from sidekick_usages.usage.ports import UsagePersistence
from sidekick_usages.usage.service import UsageCheckService

_EXIT_OK = 0
_EXIT_INVALID_INVOCATION = 2
_EXIT_INTERNAL_FAILURE = 3


def main(argv: Sequence[str] | None = None) -> int:
    """Run exactly one global concurrent usage lookup wave."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments:
        return _EXIT_INVALID_INVOCATION
    try:
        _run_lookup(
            discover_application_paths(),
            SystemClock(),
            os.environ,
        )
    except PersistenceError as error:
        _write_failure(error.code)
        return _EXIT_INTERNAL_FAILURE
    except Exception:
        _write_failure(UsageLookupFailure.INTERNAL)
        return _EXIT_INTERNAL_FAILURE
    return _EXIT_OK


def _run_lookup(
    paths: ApplicationPaths,
    clock: Clock,
    environment: Mapping[str, str],
) -> None:
    persistence = PersistenceService(
        paths,
        maintenance_quiescent=_maintenance_quiescent,
    )
    store = persistence.open_store()
    refresh_transactions = CredentialRefreshTransactions(
        store,
        paths.credential_refresh,
    )
    with refresh_transactions.hold_lifecycle():
        refresh_transactions.recover()
    providers = build_provider_registry(clock)
    resolver = credential_resolver_for(
        store,
        persistence.private_credentials,
        managed_factories=compose_managed_credential_factories(
            paths,
            persistence,
            store,
            clock,
            environment,
        ),
    )
    with ExitStack() as resources:
        http = resources.enter_context(HttpClient(clock=clock))
        refresh = CredentialRefreshCoordinator(
            store,
            http,
            providers,
            refresh_transactions,
            clock=clock,
            resolver=resolver,
        )
        service = UsageCheckService(
            store,
            http,
            providers,
            CredentialService(
                store,
                http,
                providers,
                refresh_coordinator=refresh,
            ),
            clock=clock,
            credential_access=AccountCredentialAccess(
                resolver,
                OperationAuthorityLocks(paths.durable_operations),
            ),
            local_activity_sources={
                ProviderId.CLAUDE: ClaudeActivity(
                    discover_claude_config_dir(environment)
                )
            },
            account_activity_sources={
                ProviderId.CODEX: CodexActivity(),
            },
            persistence=UsagePersistence(
                activity=ActivitySnapshotStore(paths.activity_snapshots),
                usage=UsageSnapshotStore(paths.usage_snapshots),
            ),
        )
        service.check(observe=_write_completion)
    _write_event(UsageLookupWorkerEvent(kind=UsageLookupEventKind.SUCCEEDED))


def _write_completion(completion: AccountLookupCompletion) -> None:
    _write_event(
        UsageLookupWorkerEvent(
            kind=(
                UsageLookupEventKind.ACCOUNT_SUCCEEDED
                if completion.failure is None
                else UsageLookupEventKind.ACCOUNT_FAILED
            ),
            account_id=completion.account_id,
            provider_id=completion.provider_id,
            fetch_failure=(
                None if completion.failure is None else completion.failure.kind
            ),
        )
    )


def _write_failure(failure: UsageLookupTerminalFailure) -> None:
    with suppress(OSError):
        _write_event(
            UsageLookupWorkerEvent(
                kind=UsageLookupEventKind.FAILED,
                failure=failure,
            )
        )


def _write_event(event: UsageLookupWorkerEvent) -> None:
    frame = encode_usage_lookup_event(event)
    try:
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()
    finally:
        clear_mutable_buffer(frame)


def _maintenance_quiescent() -> bool:
    return True


if __name__ == "__main__":
    sys.exit(main())

"""Provider-neutral serialized saved-credential refresh."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from sidekick_usages.clock import Clock
from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
    Credentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.authorities import (
    AuthenticatedSavedAccount,
    CredentialResolver,
    EmbeddedAccountResolver,
)
from sidekick_usages.credentials.codex import CodexCredentialCoordinator
from sidekick_usages.credentials.models import (
    CredentialRefreshResult,
    CredentialRefreshSuccess,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.credential_refresh import (
    CredentialRefreshLease,
    CredentialRefreshPersistence,
    CredentialRefreshTargetUnavailableError,
    CredentialRefreshUnstableError,
)
from sidekick_usages.persistence.private_bundle_writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.providers.base import (
    CredentialStageReader,
    Provider,
    ProviderAuthenticatedAccount,
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureCause,
    ProviderFailureKind,
    RefreshResult,
    RefreshSuccess,
)


@runtime_checkable
class StagedCredentialRefreshProvider(Protocol):
    """Provider capability that writes only within a managed child home."""

    def refresh_credentials_in_stage(
        self,
        account: ProviderAuthenticatedAccount,
        http: HttpClient,
        stage_home: Path,
        stage_reader: CredentialStageReader,
    ) -> RefreshResult:
        """Return replacement credentials using only ``stage_home``."""


@dataclass(frozen=True, slots=True)
class _LeaseCredentialStageReader:
    """Bind one provider read to its persistence-owned refresh lease."""

    persistence: CredentialRefreshPersistence
    lease: CredentialRefreshLease

    def read(self) -> bytes | None:
        """Read the one qualified child-produced credential file."""
        return self.persistence.read_provider_stage(self.lease)


class CredentialRefreshReason(StrEnum):
    """Closed reasons that may request a saved-credential refresh."""

    SCHEDULED_DUE = "scheduled_due"
    ACCESS_REJECTED = "access_rejected"
    CREDENTIAL_REQUIRED = "credential_required"
    OPERATOR_FORCED = "operator_forced"


class CredentialRefreshCoordinator:
    """Coordinate one saved refresh through its persistence boundary."""

    def __init__(
        self,
        store: AccountStore,
        http: HttpClient,
        providers: Mapping[ProviderId, Provider],
        persistence: CredentialRefreshPersistence,
        *,
        clock: Clock,
        codex: CodexCredentialCoordinator | None = None,
        resolver: CredentialResolver | None = None,
    ) -> None:
        """Bind refresh policy to provider and persistence capabilities."""
        self._store = store
        self._http = http
        self._providers = providers
        self._persistence = persistence
        self._clock = clock
        self._codex = codex
        self._resolver = resolver
        self._embedded_resolver = EmbeddedAccountResolver()

    def refresh(
        self,
        *,
        label: AccountLabel,
        reason: CredentialRefreshReason,
    ) -> CredentialRefreshResult:
        """Refresh one exact saved account for one closed caller reason."""
        observed = self._store.read_fresh(label)
        if observed is not None and isinstance(
            observed.credentials,
            ClaudeSetupTokenCredentials,
        ):
            return _setup_token_failure()
        with self._persistence.hold_lifecycle():
            return self._refresh_held(
                label=label,
                reason=reason,
                observed=observed,
            )

    def _refresh_held(
        self,
        *,
        label: AccountLabel,
        reason: CredentialRefreshReason,
        observed: Account | None,
    ) -> CredentialRefreshResult:
        """Refresh while participating in lifecycle exclusion."""
        self._persistence.recover()
        started_at = self._clock.now()
        try:
            with self._persistence.hold_stable(
                label=label,
                reason=reason.value,
                started_at=started_at,
            ) as lease:
                selected = self._provider_for(lease.account)
                if isinstance(selected, ProviderFailure):
                    self._persistence.finish_without_exchange(lease)
                    return selected
                return self._refresh_stable(
                    observed,
                    selected,
                    lease,
                    reason,
                )
        except CredentialRefreshTargetUnavailableError as error:
            return _target_unavailable_failure(error.account)
        except CredentialRefreshUnstableError as error:
            return ProviderFailure(
                provider_id=_provider_id(error.account),
                kind=ProviderFailureKind.UNREADABLE,
                message=(
                    "The refresh target changed repeatedly; retry the "
                    "operation."
                ),
                action_required=False,
            )

    def _provider_for(self, account: Account) -> Provider | ProviderFailure:
        """Return the registered provider after companion-state checks."""
        provider = self._providers.get(account.provider_id)
        if provider is None:
            return ProviderFailure(
                provider_id=account.provider_id,
                kind=ProviderFailureKind.UNSUPPORTED,
                message=f"Provider '{account.provider_id}' is not registered.",
            )
        if (
            account.provider_id is ProviderId.CODEX
            and account.codex_home is not None
            and self._codex is None
        ):
            return ProviderFailure(
                provider_id=ProviderId.CODEX,
                kind=ProviderFailureKind.UNSUPPORTED,
                message="Codex private refresh coordination is unavailable.",
            )
        return provider

    def _refresh_stable(
        self,
        observed: Account | None,
        provider: Provider,
        lease: CredentialRefreshLease,
        reason: CredentialRefreshReason,
    ) -> CredentialRefreshResult:
        """Exchange only after lock and fresh credential agreement."""
        if (
            reason is not CredentialRefreshReason.OPERATOR_FORCED
            and observed is not None
            and lease.expected_credentials != observed.credentials
        ):
            current = self._persistence.finish_without_exchange(lease)
            if current is None:
                return ProviderFailure(
                    provider_id=lease.account.provider_id,
                    kind=ProviderFailureKind.MISSING,
                    message="The refresh target no longer exists.",
                )
            return CredentialRefreshSuccess(current.label)
        try:
            with self._open_account(lease.account) as authenticated:
                if isinstance(provider, StagedCredentialRefreshProvider):
                    stage_home = self._persistence.prepare_provider_stage(
                        lease
                    )
                    refreshed = provider.refresh_credentials_in_stage(
                        authenticated,
                        self._http,
                        stage_home,
                        _LeaseCredentialStageReader(
                            self._persistence,
                            lease,
                        ),
                    )
                else:
                    refreshed = provider.refresh_credentials(
                        authenticated,
                        self._http,
                    )
        except ProviderBoundaryError as error:
            refreshed = error.failure
        completed_at = self._clock.now()
        if isinstance(refreshed, ProviderFailure):
            return self._finish_failure(lease, refreshed, completed_at)
        prepared = self._prepare_commit(lease, refreshed, completed_at)
        if isinstance(prepared, ProviderFailure):
            return self._finish_failure(lease, prepared, completed_at)
        credentials, plan, private_bundle = prepared
        committed = self._persistence.commit_success(
            lease,
            credentials,
            plan,
            completed_at,
            private_bundle=private_bundle,
        )
        if committed is None:
            return ProviderFailure(
                provider_id=lease.account.provider_id,
                kind=ProviderFailureKind.MISSING,
                message="The refresh target no longer exists.",
            )
        return CredentialRefreshSuccess(committed.label)

    def _open_account(
        self,
        account: Account,
    ) -> AbstractContextManager[AuthenticatedSavedAccount]:
        """Open one refresh credential lease at the provider boundary."""
        if self._resolver is None:
            return self._embedded_resolver.open(account)
        saved = next(
            (
                candidate
                for candidate in self._store.saved_accounts()
                if candidate.provider_id is account.provider_id
                and candidate.label == account.label
            ),
            None,
        )
        if saved is None:
            raise CredentialRefreshTargetUnavailableError(account)
        return self._resolver.open(saved)

    def _finish_failure(
        self,
        lease: CredentialRefreshLease,
        failure: ProviderFailure,
        completed_at: datetime,
    ) -> CredentialRefreshResult:
        """Persist a current failure or prefer newer durable authority."""
        current = self._persistence.persist_failure_if_current(
            lease,
            failure.message,
            completed_at,
        )
        if (
            current is not None
            and current.credentials != lease.expected_credentials
        ):
            return CredentialRefreshSuccess(current.label)
        return failure

    def _prepare_commit(
        self,
        lease: CredentialRefreshLease,
        refreshed: RefreshSuccess,
        completed_at: datetime,
    ) -> (
        tuple[
            Credentials,
            str | None,
            PreparedPrivateBundleWrite | None,
        ]
        | ProviderFailure
    ):
        """Prepare any provider-owned durable companion state."""
        credentials = refreshed.credentials
        plan = refreshed.plan
        if (
            lease.account.provider_id is not ProviderId.CODEX
            or lease.account.codex_home is None
        ):
            return credentials, plan, None
        if not isinstance(credentials, CodexCredentials):
            raise AssertionError("Codex refresh returned wrong credentials.")
        if self._codex is None:
            raise AssertionError("Codex refresh coordinator is missing.")
        prepared = self._codex.prepare_refresh(
            lease.account,
            credentials,
            plan,
            reference_time=completed_at,
        )
        if isinstance(prepared, ProviderFailure):
            return prepared
        return (
            prepared.credentials,
            prepared.plan,
            prepared.private_bundle,
        )


def _setup_token_failure() -> ProviderFailure:
    """Return the closed manual action for non-rotating Claude tokens."""
    return ProviderFailure(
        provider_id=ProviderId.CLAUDE,
        kind=ProviderFailureKind.MISSING,
        message="Claude rejected the saved setup token.",
        cause=ProviderFailureCause.MISSING_REFRESH_CREDENTIAL,
    )


def _target_unavailable_failure(account: Account | None) -> ProviderFailure:
    """Classify one terminal state from persistence's fresh authority."""
    if account is not None and isinstance(
        account.credentials,
        ClaudeSetupTokenCredentials,
    ):
        return _setup_token_failure()
    return ProviderFailure(
        provider_id=_provider_id(account),
        kind=ProviderFailureKind.MISSING,
        message="The refresh target no longer exists or cannot rotate.",
    )


def _provider_id(account: Account | None) -> ProviderId:
    """Return fresh provider identity or the provider-neutral fallback."""
    return ProviderId.CLAUDE if account is None else account.provider_id


__all__ = [
    "CredentialRefreshCoordinator",
    "CredentialRefreshPersistence",
    "CredentialRefreshReason",
    "StagedCredentialRefreshProvider",
]

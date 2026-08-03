"""Exact target authority classification and short-lived Claude leases."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialHealth,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.models import ClaudeSetupTokenCredentials
from sidekick_usages.core.selection.policy import protected_selection_enabled
from sidekick_usages.core.selection.types import SelectionCode
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.authorities import (
    AuthenticatedSavedAccount,
    AuthorizedCredentialResolver,
    SavedAccountSource,
)
from sidekick_usages.credentials.claude.activation.service import (
    ClaudeActivationService,
)
from sidekick_usages.credentials.claude.authority.resolver import (
    ClaudeManagedCredentialResolver,
)
from sidekick_usages.persistence.supervisor.authority import (
    ProviderMutationAuthority,
)
from sidekick_usages.providers.claude.auth.generation import (
    claude_access_token_generation,
)
from sidekick_usages.providers.claude.credentials import (
    require_claude_credentials,
)


class ClaudeAuthorityMode(StrEnum):
    """Closed target modes with distinct native mutation policy."""

    SETUP = "setup"
    REFRESHABLE = "refreshable"


class ClaudeSelectedAccessError(RuntimeError):
    """Reject unavailable, changed, or unsupported selected authority."""

    def __init__(
        self,
        message: str,
        code: SelectionCode = SelectionCode.AUTHORITY_PROOF_FAILED,
    ) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True, kw_only=True)
class ClaudePreparedAuthority:
    """Secret-free exact target proof retained across worker phases."""

    account_id: SidekickAccountId
    authority_id: AuthorityId
    generation: AuthorityGeneration
    mode: ClaudeAuthorityMode


class ClaudeAccessLease:
    """Expose one mutable access-token copy only inside an active context."""

    __slots__ = ("_account", "_closed", "prepared")

    def __init__(
        self,
        prepared: ClaudePreparedAuthority,
        account: AuthenticatedSavedAccount,
    ) -> None:
        self.prepared = prepared
        self._account: AuthenticatedSavedAccount | None = account
        self._closed = False

    def oauth_buffer(self) -> bytearray:
        """Return one caller-owned mutable copy of the exact access token."""
        if self._closed or self._account is None:
            raise ClaudeSelectedAccessError(
                "The selected Claude access lease is closed."
            )
        credentials = require_claude_credentials(
            self._account.lease.account
        )
        return bytearray(credentials.access_token, "utf-8")

    def close(self) -> None:
        """Release the authenticated account reference exactly once."""
        self._account = None
        self._closed = True

    def __repr__(self) -> str:
        """Return no credential or target identity."""
        return "<ClaudeAccessLease redacted>"


class ClaudeSelectedAccessLeaseService:
    """Classify and open only the exact committed Claude target."""

    def __init__(
        self,
        accounts: SavedAccountSource,
        stored: AuthorizedCredentialResolver,
        managed: ClaudeManagedCredentialResolver,
        activation: ClaudeActivationService,
        clock: Clock,
    ) -> None:
        self._accounts = accounts
        self._stored = stored
        self._managed = managed
        self._activation = activation
        self._clock = clock

    def prevalidate(
        self,
        account_id: SidekickAccountId,
        authority: ProviderMutationAuthority,
    ) -> ClaudePreparedAuthority:
        """Prove target mode, health, expiry, and generation without commit."""
        authority.require(ProviderId.CLAUDE)
        account = self._account(account_id)
        claude = account.authority
        if not isinstance(claude, ClaudeAccountAuthority):
            raise ClaudeSelectedAccessError(
                "The selected Claude authority is invalid."
            )
        setup = claude.setup_token
        if setup is not None and not protected_selection_enabled(
            ProviderId.CLAUDE
        ):
            raise ClaudeSelectedAccessError(
                "Protected Claude selection remains disabled.",
                SelectionCode.UNSUPPORTED_SESSION_CAPABILITY,
            )
        operation_authority = authority.account(account_id)
        if setup is None:
            subscription = claude.subscription
            if not isinstance(subscription, ClaudeManagedLoginAuthority):
                raise ClaudeSelectedAccessError(
                    "The selected Claude authority is unsupported."
                )
            generation = self._activation.prevalidate(
                account_id,
                authority,
            )
            return ClaudePreparedAuthority(
                account_id=account_id,
                authority_id=subscription.authority_id,
                generation=generation,
                mode=ClaudeAuthorityMode.REFRESHABLE,
            )
        if setup.health is not CredentialHealth.HEALTHY or (
            setup.expires_at is not None
            and setup.expires_at <= self._clock.now()
        ):
            raise ClaudeSelectedAccessError(
                "The selected Claude setup authority is unavailable."
            )
        with self._stored.open_authorized(
            account,
            operation_authority,
        ) as authenticated:
            credentials = require_claude_credentials(
                authenticated.lease.account
            )
            if not isinstance(credentials, ClaudeSetupTokenCredentials):
                raise ClaudeSelectedAccessError(
                    "The selected Claude setup authority changed."
                )
            generation = claude_access_token_generation(
                credentials.access_token
            )
        return ClaudePreparedAuthority(
            account_id=account_id,
            authority_id=setup.authority_id,
            generation=generation,
            mode=ClaudeAuthorityMode.SETUP,
        )

    @contextmanager
    def open_committed(
        self,
        operation_id: OperationId,
        prepared: ClaudePreparedAuthority,
        authority: ProviderMutationAuthority,
    ) -> Iterator[ClaudeAccessLease]:
        """Commit mode policy, then open the exact proved target lease."""
        self._require_current(prepared, authority)
        if prepared.mode is ClaudeAuthorityMode.REFRESHABLE:
            selected = self._activation.activate(
                operation_id,
                prepared.account_id,
                authority,
                expected_target_generation=prepared.generation,
            )
            if selected.runtime_generation != prepared.generation:
                raise ClaudeSelectedAccessError(
                    "The committed Claude generation does not match."
                )
        with self._open_authorized(prepared, authority) as lease:
            yield lease

    @contextmanager
    def open_proven(
        self,
        prepared: ClaudePreparedAuthority,
        authority: ProviderMutationAuthority,
    ) -> Iterator[ClaudeAccessLease]:
        """Open an already-proven target without native mutation."""
        self._require_current(prepared, authority)
        with self._open_authorized(prepared, authority) as lease:
            yield lease

    def _require_current(
        self,
        prepared: ClaudePreparedAuthority,
        authority: ProviderMutationAuthority,
    ) -> None:
        current = self.prevalidate(prepared.account_id, authority)
        if current != prepared:
            raise ClaudeSelectedAccessError(
                "The selected Claude authority changed."
            )

    @contextmanager
    def _open_authorized(
        self,
        prepared: ClaudePreparedAuthority,
        authority: ProviderMutationAuthority,
    ) -> Iterator[ClaudeAccessLease]:
        account = self._account(prepared.account_id)
        operation_authority = authority.account(prepared.account_id)
        if prepared.mode is ClaudeAuthorityMode.REFRESHABLE:
            authenticated_context = self._managed.open_native_authorized(
                account,
                prepared.generation,
                operation_authority,
            )
        else:
            authenticated_context = self._stored.open_authorized(
                account,
                operation_authority,
            )
        with authenticated_context as authenticated:
            lease = ClaudeAccessLease(prepared, authenticated)
            try:
                yield lease
            finally:
                lease.close()

    def _account(self, account_id: SidekickAccountId) -> SavedAccount:
        matches = tuple(
            account
            for account in self._accounts.saved_accounts()
            if account.account_id == account_id
            and account.provider_id is ProviderId.CLAUDE
        )
        if len(matches) != 1:
            raise ClaudeSelectedAccessError(
                "The selected Claude account is unavailable."
            )
        return matches[0]

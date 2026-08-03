"""Transactional account store over the no-secret account index."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Self

from sidekick_usages.core.accounts.identifiers import (
    new_authority_id,
    new_sidekick_account_id,
)
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import (
    AccountIdFactory,
    AuthorityIdFactory,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    Credentials,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.persistence.accounts.index import (
    AccountIndex,
    safe_error_code,
    saved_account_from_runtime,
)
from sidekick_usages.persistence.accounts.runtime_bridge import (
    CredentialAuthorityUnavailableError,
    copy_runtime_account,
    credential_authority_reference,
    merge_claude_authority,
    runtime_account_from_saved,
    saved_account_from_runtime_state,
)
from sidekick_usages.persistence.accounts.transactions.models import (
    AccountPersistenceState,
)
from sidekick_usages.persistence.accounts.transactions.service import (
    AccountTransactionCoordinator,
)
from sidekick_usages.persistence.accounts.transactions.types import (
    AccountFilesystemFactory,
    AccountLockFactory,
)
from sidekick_usages.persistence.credentials.refresh.merge import (
    CredentialRefreshMerge,
    CredentialRefreshSuccessMerge,
)
from sidekick_usages.persistence.credentials.repository import (
    authority_for_account,
)
from sidekick_usages.persistence.credentials.transactions.transaction import (
    CredentialSourceGuard,
)
from sidekick_usages.persistence.credentials.transitions.claude import (
    managed_claude_transition_matches,
    reconciled_claude_setup_transition_matches,
    stored_claude_setup_transition_matches,
    stored_claude_transition_matches,
)
from sidekick_usages.persistence.credentials.transitions.codex import (
    managed_codex_transition_matches,
    stored_codex_transition_matches,
)
from sidekick_usages.persistence.errors import SourceChangedError
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.credential import (
    StoredCredentialAuthority,
)
from sidekick_usages.persistence.private.bundles.writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)


class AccountStore:
    """Stable-ID account store for the current schema."""

    def __init__(
        self,
        account_path: Path,
        private_credentials: PrivateCredentialTree,
        *,
        lock_factory: AccountLockFactory = PersistenceLock,
        filesystem_factory: AccountFilesystemFactory = PersistenceFilesystem,
        account_id_factory: AccountIdFactory = new_sidekick_account_id,
        authority_id_factory: AuthorityIdFactory = new_authority_id,
    ) -> None:
        if not account_path.is_absolute():
            raise ValueError("Account authority path must be absolute.")
        self.path = account_path
        self._transactions = AccountTransactionCoordinator(
            account_path,
            private_credentials,
            lock_factory=lock_factory,
            filesystem_factory=filesystem_factory,
        )
        self._account_id_factory = account_id_factory
        self._authority_id_factory = authority_id_factory
        self._state = AccountPersistenceState.empty()
        self._loaded = False

    def load(self) -> Self:
        """Recover and load the current account index once."""
        if self._loaded:
            return self
        self._state = self._transactions.load()
        self._loaded = True
        return self

    def __iter__(self) -> Iterator[Account]:
        """Iterate defensive runtime account copies."""
        self._require_loaded()
        return iter(
            tuple(
                copy_runtime_account(account)
                for account in self._state.runtime.values()
            )
        )

    def __len__(self) -> int:
        """Return the managed account count."""
        self._require_loaded()
        return len(self._state.index)

    def __contains__(self, label: object) -> bool:
        """Return whether any provider owns an exact label."""
        self._require_loaded()
        return any(account.label == label for account in self._state.index)

    def saved_accounts(
        self,
        provider_id: ProviderId | None = None,
    ) -> tuple[SavedAccount, ...]:
        """Return secret-free accounts in insertion order."""
        self._require_loaded()
        return tuple(
            account
            for account in self._state.index
            if provider_id is None or account.provider_id is provider_id
        )

    def read_saved(
        self,
        account_id: SidekickAccountId,
    ) -> SavedAccount | None:
        """Reopen and return one secret-free account by stable ID."""
        self._require_loaded()
        self._reload()
        return self._state.index.get(account_id)

    def resolve_account_id(
        self,
        provider_id: ProviderId,
        label: AccountLabel,
    ) -> SidekickAccountId | None:
        """Resolve one exact provider-qualified label to its stable ID."""
        self._require_loaded()
        account = self._state.index.resolve(provider_id, label)
        return account.account_id if account is not None else None

    def get(
        self,
        label: str,
        *,
        provider_id: ProviderId | None = None,
    ) -> Account | None:
        """Return one exact account or reject cross-provider ambiguity."""
        self._require_loaded()
        account = self._saved_for_label(
            AccountLabel(label),
            provider_id=provider_id,
        )
        if account is None:
            return None
        runtime = self._state.runtime.get(account.account_id)
        if runtime is None:
            raise CredentialAuthorityUnavailableError
        return copy_runtime_account(runtime)

    def read_fresh(
        self,
        label: AccountLabel,
        *,
        provider_id: ProviderId | None = None,
    ) -> Account | None:
        """Reopen and adopt the complete v3 index under its lock."""
        self._require_loaded()
        self._reload()
        return self.get(str(label), provider_id=provider_id)

    def find_by_token(
        self,
        provider_id: ProviderId,
        token: str,
    ) -> Account | None:
        """Find one runtime account by provider and exact token."""
        self._require_loaded()
        for account in self._state.runtime.values():
            if (
                account.provider_id is provider_id
                and account.access_token == token
            ):
                return copy_runtime_account(account)
        return None

    def filter_by_provider(self, provider_id: ProviderId) -> list[Account]:
        """Return defensive copies for one provider."""
        self._require_loaded()
        return [
            copy_runtime_account(account)
            for account in self._state.runtime.values()
            if account.provider_id is provider_id
        ]

    def persist(self, account: Account) -> None:
        """Insert or update one complete account."""
        self.persist_credentials(account)

    def persist_credentials(
        self,
        account: Account,
        *,
        previous_label: str | None = None,
        private_bundle: PreparedPrivateBundleWrite | None = None,
        source_guard: CredentialSourceGuard | None = None,
    ) -> None:
        """Persist metadata and its protected credential authority together."""
        self._require_loaded()
        previous = self._existing_for_update(account, previous_label)
        candidate, authority, expected_payload = self._updated_saved(
            previous,
            account,
        )
        index = AccountIndex(tuple(self._state.index))
        if previous is None:
            index.add(candidate)
        else:
            index.replace(candidate)
        runtime = dict(self._state.runtime)
        runtime[candidate.account_id] = copy_runtime_account(account)
        authority_bundle = self._authority_bundle(
            authority,
            expected_payload,
        )
        bundles = (
            (authority_bundle,)
            if private_bundle is None
            else (authority_bundle, private_bundle)
        )
        self._commit(
            index,
            runtime,
            bundles,
            source_guard=source_guard,
        )

    def persist_state(
        self,
        account: SavedAccount,
        *,
        expected: SavedAccount | None = None,
    ) -> None:
        """Persist only one account's no-secret mutable index state."""
        self._require_loaded()
        with self._transactions.transaction() as transaction:
            self._state = transaction.state
            current = self._state.index.get(account.account_id)
            authority_matches = current is not None and (
                current.authority == account.authority
                or managed_claude_transition_matches(current, account)
                or managed_codex_transition_matches(current, account)
            )
            if (
                current is None
                or (expected is not None and current != expected)
                or current.provider_id is not account.provider_id
                or current.label != account.label
                or not authority_matches
            ):
                raise SourceChangedError
            runtime = dict(self._state.runtime)
            current_runtime = runtime.get(account.account_id)
            if current_runtime is not None:
                runtime[account.account_id] = runtime_account_from_saved(
                    account,
                    current_runtime.credentials,
                )
            index = AccountIndex(tuple(self._state.index))
            index.replace(account)
            self._state = transaction.commit(
                index,
                runtime,
                (),
            )

    def migrate_stored_authority(
        self,
        account: SavedAccount,
        *,
        expected: SavedAccount,
    ) -> None:
        """Atomically replace one stored authority with managed state."""
        self._require_loaded()
        with self._transactions.transaction() as transaction:
            self._state = transaction.state
            current = self._state.index.get(account.account_id)
            if (
                current != expected
                or current is None
                or not (
                    stored_codex_transition_matches(current, account)
                    or stored_claude_transition_matches(current, account)
                )
            ):
                raise SourceChangedError
            index = AccountIndex(tuple(self._state.index))
            index.replace(account)
            runtime = dict(self._state.runtime)
            runtime.pop(account.account_id, None)
            self._state = transaction.commit(
                index,
                runtime,
                (),
            )

    def restore_claude_setup_authority(
        self,
        account: SavedAccount,
        *,
        expected: SavedAccount,
    ) -> None:
        """Atomically remove one false managed Claude association."""
        self._require_loaded()
        with self._transactions.transaction() as transaction:
            self._state = transaction.state
            current = self._state.index.get(account.account_id)
            if (
                current != expected
                or current is None
                or not reconciled_claude_setup_transition_matches(
                    current,
                    account,
                )
            ):
                raise SourceChangedError
            index = AccountIndex(tuple(self._state.index))
            index.replace(account)
            runtime = dict(self._state.runtime)
            runtime.pop(account.account_id, None)
            self._state = transaction.commit(
                index,
                runtime,
                (),
            )

    def persist_claude_setup_token(
        self,
        candidate: SavedAccount,
        authority: StoredCredentialAuthority,
        *,
        expected: SavedAccount,
    ) -> None:
        """Atomically attach or renew one protected Claude setup token."""
        self._require_loaded()
        current = self._state.index.get(candidate.account_id)
        credentials = authority.credentials
        if (
            current != expected
            or current is None
            or not isinstance(credentials, ClaudeSetupTokenCredentials)
            or not stored_claude_setup_transition_matches(
                current,
                candidate,
                authority,
            )
        ):
            raise SourceChangedError
        index = AccountIndex(tuple(self._state.index))
        index.replace(candidate)
        runtime = dict(self._state.runtime)
        current_runtime = runtime.get(candidate.account_id)
        if current_runtime is not None and isinstance(
            current_runtime.credentials,
            ClaudeSetupTokenCredentials,
        ):
            runtime[candidate.account_id] = runtime_account_from_saved(
                candidate,
                credentials,
            )
        expected_payload = self._state.authority_payloads.get(
            (candidate.account_id, authority.authority_id)
        )
        self._commit(
            index,
            runtime,
            (self._authority_bundle(authority, expected_payload),),
        )

    def merge_credential_refresh(
        self,
        label: AccountLabel,
        expected_credentials: Credentials,
        update: CredentialRefreshMerge,
    ) -> Account | None:
        """Rebase one refresh result onto freshly reopened v3 state."""
        self._require_loaded()
        with self._transactions.transaction() as transaction:
            self._state = transaction.state
            saved = self._saved_for_label(
                label,
                provider_id=expected_credentials.provider_id,
            )
            if saved is None:
                return None
            current = self._state.runtime[saved.account_id]
            if current.credentials != expected_credentials:
                return copy_runtime_account(current)
            candidate = copy_runtime_account(current)
            extra_bundle: PreparedPrivateBundleWrite | None = None
            if isinstance(update, CredentialRefreshSuccessMerge):
                candidate.credentials = update.credentials
                if update.plan is not None:
                    candidate.plan = update.plan
                candidate.last_refresh_at = update.completed_at
                candidate.last_refresh_status = RefreshStatus.OK
                candidate.last_refresh_error = None
                extra_bundle = update.private_bundle
            else:
                candidate.last_refresh_at = update.completed_at
                candidate.last_refresh_status = RefreshStatus.FAILED
                candidate.last_refresh_error = update.message
                index = AccountIndex(tuple(self._state.index))
                index.replace(
                    saved_account_from_runtime_state(saved, candidate)
                )
                runtime = dict(self._state.runtime)
                runtime[saved.account_id] = candidate
                self._state = transaction.commit(
                    index,
                    runtime,
                    (),
                )
                return copy_runtime_account(candidate)
            index, runtime, bundle = self._candidate_update(saved, candidate)
            bundles = (
                (bundle,) if extra_bundle is None else (bundle, extra_bundle)
            )
            self._state = transaction.commit(
                index,
                runtime,
                bundles,
            )
            return copy_runtime_account(runtime[saved.account_id])

    def remove_saved(
        self,
        account_id: SidekickAccountId,
        *,
        expected: SavedAccount,
    ) -> None:
        """Remove one exact unchanged saved account and its authorities."""
        self._require_loaded()
        current = self._state.index.get(account_id)
        if current != expected:
            raise SourceChangedError
        index = AccountIndex(tuple(self._state.index))
        index.remove(account_id)
        runtime = dict(self._state.runtime)
        runtime.pop(account_id, None)
        self._commit(index, runtime, ())

    def rename_saved(
        self,
        account_id: SidekickAccountId,
        label: AccountLabel,
        *,
        expected: SavedAccount,
    ) -> SavedAccount:
        """Rename one unchanged account while preserving its stable ID."""
        self._require_loaded()
        current = self._state.index.get(account_id)
        collision = self._state.index.resolve(expected.provider_id, label)
        if current != expected or (
            collision is not None and collision.account_id != account_id
        ):
            raise SourceChangedError
        candidate = expected.renamed(label)
        index = AccountIndex(tuple(self._state.index))
        index.replace(candidate)
        runtime = dict(self._state.runtime)
        current_runtime = runtime.get(account_id)
        if current_runtime is not None:
            runtime[account_id] = copy_runtime_account(
                current_runtime,
                label=label,
            )
        self._commit(index, runtime, ())
        return candidate

    def reset_all(self) -> int:
        """Remove every account and its referenced private authorities."""
        self._require_loaded()
        removed = len(self._state.index)
        if removed:
            self._commit(AccountIndex(), {}, ())
        return removed

    def recover_credentials(self) -> bool:
        """Recover an interrupted credential/index transaction."""
        self._require_loaded()
        recovered = self._transactions.recover()
        if recovered is None:
            return False
        self._state = recovered
        return True

    def generate_label(
        self,
        provider_id: ProviderId,
        plan: str,
    ) -> AccountLabel:
        """Return the smallest unused provider-qualified generated label."""
        self._require_loaded()
        plan_component = (plan or "account").lower().replace(" ", "-")
        base = f"{provider_id}-{plan_component}"
        suffix = 1
        while (
            self._state.index.resolve(
                provider_id,
                AccountLabel(f"{base}-{suffix}"),
            )
            is not None
        ):
            suffix += 1
        return AccountLabel(f"{base}-{suffix}")

    def _saved_for_label(
        self,
        label: AccountLabel,
        *,
        provider_id: ProviderId | None,
    ) -> SavedAccount | None:
        if provider_id is not None:
            return self._state.index.resolve(provider_id, label)
        return self._state.index.resolve_label(label)

    def _existing_for_update(
        self,
        account: Account,
        previous_label: str | None,
    ) -> SavedAccount | None:
        if previous_label is not None:
            previous = self._saved_for_label(
                AccountLabel(previous_label),
                provider_id=account.provider_id,
            )
            if previous is None:
                raise SourceChangedError
            target = self._state.index.resolve(
                account.provider_id,
                account.label,
            )
            if target is not None and target.account_id != previous.account_id:
                raise ValueError("Replacement account label already exists.")
            return previous
        return self._state.index.resolve(account.provider_id, account.label)

    def _updated_saved(
        self,
        previous: SavedAccount | None,
        account: Account,
    ) -> tuple[SavedAccount, StoredCredentialAuthority, bytes | None]:
        account_id = (
            previous.account_id
            if previous is not None
            else self._account_id_factory()
        )
        reference = (
            credential_authority_reference(previous, account.credentials)
            if previous is not None
            else None
        )
        authority_id = reference or self._authority_id_factory()
        candidate = saved_account_from_runtime(
            account,
            account_id=account_id,
            authority_id=authority_id,
        )
        candidate = merge_claude_authority(previous, candidate)
        candidate = replace(
            candidate,
            last_refresh_error_code=safe_error_code(
                account.last_refresh_error
            ),
            last_heartbeat_error_code=safe_error_code(
                account.last_heartbeat_error
            ),
        )
        authority = authority_for_account(
            account,
            account_id=account_id,
            authority_id=authority_id,
        )
        expected = self._state.authority_payloads.get(
            (account_id, authority_id)
        )
        return candidate, authority, expected

    def _authority_bundle(
        self,
        authority: StoredCredentialAuthority,
        expected_payload: bytes | None,
    ) -> PreparedPrivateBundleWrite:
        return self._transactions.prepare_authority_write(
            authority,
            expected_payload,
        )

    def _candidate_update(
        self,
        saved: SavedAccount,
        account: Account,
    ) -> tuple[
        AccountIndex,
        dict[SidekickAccountId, Account],
        PreparedPrivateBundleWrite,
    ]:
        candidate, authority, expected = self._updated_saved(saved, account)
        index = AccountIndex(tuple(self._state.index))
        index.replace(candidate)
        runtime = dict(self._state.runtime)
        runtime[saved.account_id] = account
        return index, runtime, self._authority_bundle(authority, expected)

    def _commit(
        self,
        index: AccountIndex,
        runtime: dict[SidekickAccountId, Account],
        bundles: tuple[PreparedPrivateBundleWrite, ...],
        *,
        source_guard: CredentialSourceGuard | None = None,
    ) -> None:
        self._state = self._transactions.commit(
            self._state,
            index,
            runtime,
            bundles,
            source_guard=source_guard,
        )

    def _reload(self) -> None:
        self._state = self._transactions.load()

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("Account store must be loaded before use.")

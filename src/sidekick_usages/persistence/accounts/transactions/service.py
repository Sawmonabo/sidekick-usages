"""Account-index commit, private-authority, and recovery coordination."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import AuthorityId, SidekickAccountId
from sidekick_usages.core.models import Account
from sidekick_usages.persistence.accounts.index import AccountIndex
from sidekick_usages.persistence.accounts.runtime_bridge import (
    active_stored_reference,
    authority_baseline_matches,
    copy_runtime_account,
    require_active_authority_kind,
    runtime_account_from_saved,
)
from sidekick_usages.persistence.accounts.transactions.models import (
    AccountPersistenceState,
)
from sidekick_usages.persistence.accounts.transactions.types import (
    AccountFilesystemFactory,
    AccountLockFactory,
    AccountPersistenceTransaction,
)
from sidekick_usages.persistence.credentials.repository import (
    CredentialAuthorityRepository,
    referenced_stored_authorities,
)
from sidekick_usages.persistence.credentials.transactions.transaction import (
    CredentialSourceGuard,
    PrivateCredentialTransaction,
)
from sidekick_usages.persistence.errors import (
    DurabilityUncertainError,
    InvalidSchemaError,
    PrivateCredentialCollisionError,
    SourceChangedError,
)
from sidekick_usages.persistence.models.account import VersionThreeDocument
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.models.credential import (
    StoredCredentialAuthority,
)
from sidekick_usages.persistence.private.bundles.references import (
    canonical_private_accounts,
)
from sidekick_usages.persistence.private.bundles.writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.account import (
    decode_version_three,
    encode_version_three,
)
from sidekick_usages.persistence.schema.authority import (
    decode_credential_authority,
)


class _AccountCommitTransaction:
    """Commit against one freshly loaded state under the account lock."""

    def __init__(
        self,
        coordinator: AccountTransactionCoordinator,
        transaction: AccountPersistenceTransaction,
        private_transaction: PrivateCredentialTransaction,
        state: AccountPersistenceState,
        source_guard: CredentialSourceGuard | None,
    ) -> None:
        self.state = state
        self._coordinator = coordinator
        self._transaction = transaction
        self._private_transaction = private_transaction
        self._source_guard = source_guard

    def commit(
        self,
        index: AccountIndex,
        runtime: dict[SidekickAccountId, Account],
        bundles: tuple[PreparedPrivateBundleWrite, ...],
    ) -> AccountPersistenceState:
        """Commit one candidate while the owning lock remains held."""
        return self._coordinator._commit_locked(
            self._transaction,
            self._private_transaction,
            self.state,
            index,
            runtime,
            bundles,
            source_guard=self._source_guard,
        )


class AccountTransactionCoordinator:
    """Own account authority loading, commit, and crash recovery."""

    def __init__(
        self,
        account_path: Path,
        private_credentials: PrivateCredentialTree,
        *,
        lock_factory: AccountLockFactory,
        filesystem_factory: AccountFilesystemFactory,
    ) -> None:
        self._filesystem = filesystem_factory(account_path)
        self._private = private_credentials
        self._repository = CredentialAuthorityRepository(private_credentials)
        self._lock_factory = lock_factory

    def load(self) -> AccountPersistenceState:
        """Recover and load the complete current account authority."""
        with self._lock_factory(self._filesystem).hold():
            self._private_transaction().recover()
            return self._read_state()

    def recover(self) -> AccountPersistenceState | None:
        """Recover interrupted credential work and return refreshed state."""
        with self._lock_factory(self._filesystem).hold():
            recovered = self._private_transaction().recover()
            return self._read_state() if recovered else None

    def prepare_authority_write(
        self,
        authority: StoredCredentialAuthority,
        expected_payload: bytes | None,
    ) -> PreparedPrivateBundleWrite:
        """Prepare one qualified protected-authority mutation."""
        return self._repository.prepare_write(
            authority,
            expected_payload=expected_payload,
        )

    def commit(
        self,
        state: AccountPersistenceState,
        index: AccountIndex,
        runtime: dict[SidekickAccountId, Account],
        bundles: tuple[PreparedPrivateBundleWrite, ...],
        *,
        source_guard: CredentialSourceGuard | None = None,
    ) -> AccountPersistenceState:
        """Commit one candidate against the exact loaded baseline."""
        with self._lock_factory(self._filesystem).hold() as transaction:
            private_transaction = self._private_transaction()
            private_transaction.recover(source_guard=source_guard)
            observed = self._read_snapshot()
            if not authority_baseline_matches(state.baseline, observed):
                raise SourceChangedError
            return self._commit_locked(
                transaction,
                private_transaction,
                state,
                index,
                runtime,
                bundles,
                source_guard=source_guard,
            )

    @contextmanager
    def transaction(
        self,
        *,
        source_guard: CredentialSourceGuard | None = None,
    ) -> Iterator[_AccountCommitTransaction]:
        """Yield a freshly loaded commit capability under the account lock."""
        with self._lock_factory(self._filesystem).hold() as transaction:
            private_transaction = self._private_transaction()
            private_transaction.recover(source_guard=source_guard)
            yield _AccountCommitTransaction(
                self,
                transaction,
                private_transaction,
                self._read_state(),
                source_guard,
            )

    def _commit_locked(
        self,
        transaction: AccountPersistenceTransaction,
        private_transaction: PrivateCredentialTransaction,
        state: AccountPersistenceState,
        index: AccountIndex,
        runtime: dict[SidekickAccountId, Account],
        bundles: tuple[PreparedPrivateBundleWrite, ...],
        *,
        source_guard: CredentialSourceGuard | None,
    ) -> AccountPersistenceState:
        document = index.document()
        payload = encode_version_three(document)
        validated = decode_version_three(payload)
        introduced, changed, displaced = self._private_changes(
            state,
            validated,
            runtime,
            bundles,
        )
        prepared = {bundle.path for bundle in bundles}
        if not (introduced | changed) <= prepared:
            missing = min(
                (introduced | changed) - prepared,
                key=lambda path: path.as_posix(),
            )
            raise PrivateCredentialCollisionError(missing.name)
        final = private_transaction.commit(
            transaction,
            payload,
            state.baseline,
            private_bundles=bundles,
            displaced_bundles=displaced,
            source_guard=source_guard,
        )
        if final.data != payload:
            raise DurabilityUncertainError(
                self._filesystem.authority_path.name
            )
        return self._state_from_runtime(
            validated,
            runtime,
            final.fingerprint,
        )

    def _private_changes(
        self,
        state: AccountPersistenceState,
        document: VersionThreeDocument,
        runtime: dict[SidekickAccountId, Account],
        bundles: tuple[PreparedPrivateBundleWrite, ...],
    ) -> tuple[set[Path], set[Path], tuple[Path, ...]]:
        old_authorities = self._authority_paths(tuple(state.index))
        new_authorities = self._authority_paths(document.accounts)
        introduced: set[Path] = new_authorities - old_authorities
        displaced: set[Path] = old_authorities - new_authorities
        changed = {
            bundle.path for bundle in bundles if bundle.path in old_authorities
        }
        old_private = canonical_private_accounts(
            state.runtime.values(),
            self._private,
        )
        new_private = canonical_private_accounts(
            runtime.values(),
            self._private,
        )
        old_paths = set(old_private)
        new_paths = set(new_private)
        introduced.update(new_paths - old_paths)
        displaced.update(old_paths - new_paths)
        changed.update(
            path
            for path in old_paths & new_paths
            if old_private[path].credentials != new_private[path].credentials
        )
        return (
            introduced,
            changed,
            tuple(sorted(displaced, key=lambda path: path.as_posix())),
        )

    def _authority_paths(
        self,
        accounts: tuple[SavedAccount, ...],
    ) -> set[Path]:
        return {
            self._repository.bundle_path(account.account_id, authority_id)
            for account in accounts
            for authority_id in referenced_stored_authorities(account)
        }

    def _read_state(self) -> AccountPersistenceState:
        snapshot = self._read_snapshot()
        if snapshot is None:
            return AccountPersistenceState.empty()
        return self._state_from_document(
            decode_version_three(snapshot.data),
            snapshot.fingerprint,
        )

    def _read_snapshot(self) -> FileSnapshot | None:
        snapshot = self._filesystem.read_authority()
        if snapshot is not None:
            decode_version_three(snapshot.data)
        return snapshot

    def _state_from_document(
        self,
        document: VersionThreeDocument,
        baseline: ExpectedAuthority,
    ) -> AccountPersistenceState:
        index = AccountIndex(document.accounts)
        runtime: dict[SidekickAccountId, Account] = {}
        payloads: dict[tuple[SidekickAccountId, AuthorityId], bytes] = {}
        for saved in index:
            for authority_id in referenced_stored_authorities(saved):
                payload = self._repository.read_payload(
                    saved.account_id,
                    authority_id,
                )
                if payload is None:
                    raise InvalidSchemaError
                authority = decode_credential_authority(payload)
                if (
                    authority.account_id != saved.account_id
                    or authority.authority_id != authority_id
                    or authority.provider_id is not saved.provider_id
                ):
                    raise InvalidSchemaError
                payloads[(saved.account_id, authority_id)] = payload
            if saved.has_managed_authority:
                continue
            active_id = active_stored_reference(saved)
            active_payload = payloads.get((saved.account_id, active_id))
            if active_payload is None:
                raise InvalidSchemaError
            active = decode_credential_authority(active_payload)
            require_active_authority_kind(saved, active)
            runtime[saved.account_id] = runtime_account_from_saved(
                saved,
                active.credentials,
            )
        return AccountPersistenceState(
            index,
            runtime,
            payloads,
            baseline,
        )

    def _state_from_runtime(
        self,
        document: VersionThreeDocument,
        runtime: dict[SidekickAccountId, Account],
        baseline: ExpectedAuthority,
    ) -> AccountPersistenceState:
        index = AccountIndex(document.accounts)
        payloads: dict[tuple[SidekickAccountId, AuthorityId], bytes] = {}
        for saved in index:
            for authority_id in referenced_stored_authorities(saved):
                payload = self._repository.read_payload(
                    saved.account_id,
                    authority_id,
                )
                if payload is None:
                    raise DurabilityUncertainError(
                        self._filesystem.authority_path.name
                    )
                payloads[(saved.account_id, authority_id)] = payload
        return AccountPersistenceState(
            index,
            {
                account_id: copy_runtime_account(account)
                for account_id, account in runtime.items()
            },
            payloads,
            baseline,
        )

    def _private_transaction(self) -> PrivateCredentialTransaction:
        return PrivateCredentialTransaction(
            self._private,
            self._filesystem.read_authority,
        )

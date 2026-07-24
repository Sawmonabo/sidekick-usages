"""Transactional runtime bridge over the no-secret account index."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from sidekick_usages.core.accounts import (
    AuthorityId,
    SavedAccount,
    SidekickAccountId,
)
from sidekick_usages.core.models import (
    Account,
    Credentials,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.persistence.account_index import (
    AccountIndex,
    AccountLabelAmbiguityError,
    legacy_error_code,
    legacy_saved_account,
)
from sidekick_usages.persistence.account_runtime_bridge import (
    ManagedAuthorityResolutionError,
    active_legacy_reference,
    authority_baseline_matches,
    copy_runtime_account,
    credential_authority_reference,
    merge_claude_authority,
    require_active_authority_kind,
    runtime_account_from_saved,
    saved_account_from_runtime_state,
)
from sidekick_usages.persistence.account_schema_v3 import (
    VersionThreeDocument,
    decode_version_three,
    encode_version_three,
)
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.credential_authorities import (
    CredentialAuthorityRepository,
    LegacyCredentialAuthority,
    authority_for_account,
    decode_credential_authority,
    referenced_legacy_authorities,
)
from sidekick_usages.persistence.credential_refresh_merge import (
    CredentialRefreshMerge,
    CredentialRefreshSuccessMerge,
)
from sidekick_usages.persistence.credential_transactions import (
    CredentialSourceGuard,
    PrivateCredentialTransaction,
)
from sidekick_usages.persistence.errors import (
    DurabilityUncertainError,
    InvalidSchemaError,
    PrivateCredentialCollisionError,
    SourceChangedError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.managed_migration import (
    AccountIdFactory,
    AuthorityIdFactory,
    new_account_id,
    new_authority_id,
)
from sidekick_usages.persistence.private_bundle_references import (
    canonical_private_accounts,
)
from sidekick_usages.persistence.private_bundle_writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)


class _AccountPersistenceTransaction(Protocol):
    """Held account-index commit capability."""

    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Commit exact account-index bytes."""


class _AccountPersistenceLock(Protocol):
    """Cooperative account-index lock."""

    def hold(
        self,
    ) -> AbstractContextManager[_AccountPersistenceTransaction]:
        """Acquire the account lock."""


type LockFactory = Callable[
    [PersistenceFilesystem],
    _AccountPersistenceLock,
]
type SnapshotReader = Callable[[], FileSnapshot | None]


class ManagedAccountStore:
    """Stable-ID runtime account bridge for schema version three."""

    def __init__(
        self,
        filesystem: PersistenceFilesystem,
        private_credentials: PrivateCredentialTree,
        snapshot_reader: SnapshotReader,
        *,
        lock_factory: LockFactory,
        account_id_factory: AccountIdFactory = new_account_id,
        authority_id_factory: AuthorityIdFactory = new_authority_id,
    ) -> None:
        self._filesystem = filesystem
        self._private = private_credentials
        self._repository = CredentialAuthorityRepository(private_credentials)
        self._snapshot_reader = snapshot_reader
        self._lock_factory = lock_factory
        self._account_id_factory = account_id_factory
        self._authority_id_factory = authority_id_factory
        self._index = AccountIndex()
        self._runtime: dict[SidekickAccountId, Account] = {}
        self._authority_payloads: dict[
            tuple[SidekickAccountId, AuthorityId],
            bytes,
        ] = {}
        self._baseline: ExpectedAuthority = AuthorityExpectation.ABSENT

    @property
    def baseline(self) -> ExpectedAuthority:
        """Return the exact loaded account-index authority."""
        return self._baseline

    def load(self, snapshot: FileSnapshot | None) -> None:
        """Adopt one validated account-index snapshot."""
        if snapshot is None:
            self._adopt(
                VersionThreeDocument(()),
                AuthorityExpectation.ABSENT,
            )
            return
        self._adopt(
            decode_version_three(snapshot.data),
            snapshot.fingerprint,
        )

    def __iter__(self) -> Iterator[Account]:
        """Iterate defensive runtime account copies."""
        return iter(
            tuple(
                copy_runtime_account(account)
                for account in self._runtime.values()
            )
        )

    def __len__(self) -> int:
        """Return the managed account count."""
        return len(self._runtime)

    def contains_label(self, label: object) -> bool:
        """Return whether any provider owns an exact label."""
        return any(
            account.label == label for account in self._runtime.values()
        )

    def saved_accounts(self) -> tuple[SavedAccount, ...]:
        """Return immutable secret-free accounts in insertion order."""
        return tuple(self._index)

    def account_id(
        self,
        provider_id: ProviderId,
        label: AccountLabel,
    ) -> SidekickAccountId | None:
        """Resolve one exact provider-qualified label to its stable ID."""
        account = self._index.resolve(provider_id, label)
        return account.account_id if account is not None else None

    def get(
        self,
        label: str,
        *,
        provider_id: ProviderId | None = None,
    ) -> Account | None:
        """Return one exact account or reject cross-provider ambiguity."""
        account = self._saved_for_label(
            AccountLabel(label),
            provider_id=provider_id,
        )
        if account is None:
            return None
        return copy_runtime_account(self._runtime[account.account_id])

    def read_fresh(
        self,
        label: AccountLabel,
        *,
        provider_id: ProviderId | None = None,
    ) -> Account | None:
        """Reopen and adopt the complete v3 index under its lock."""
        with self._lock_factory(self._filesystem).hold():
            snapshot = self._snapshot_reader()
            self.load(snapshot)
            return self.get(str(label), provider_id=provider_id)

    def find_by_token(
        self,
        provider_id: ProviderId,
        token: str,
    ) -> Account | None:
        """Find one transitional runtime account by exact token."""
        for account in self._runtime.values():
            if (
                account.provider_id is provider_id
                and account.access_token == token
            ):
                return copy_runtime_account(account)
        return None

    def filter_by_provider(self, provider_id: ProviderId) -> list[Account]:
        """Return defensive copies for one provider."""
        return [
            copy_runtime_account(account)
            for account in self._runtime.values()
            if account.provider_id is provider_id
        ]

    def persist_credentials(
        self,
        account: Account,
        *,
        previous_label: str | None = None,
        private_bundle: PreparedPrivateBundleWrite | None = None,
        source_guard: CredentialSourceGuard | None = None,
    ) -> None:
        """Persist metadata and its protected credential authority together."""
        previous = self._existing_for_update(account, previous_label)
        candidate, authority, expected_payload = self._updated_saved(
            previous,
            account,
        )
        index = AccountIndex(tuple(self._index))
        if previous is None:
            index.add(candidate)
        else:
            index.replace(candidate)
        runtime = dict(self._runtime)
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
        with self._lock_factory(self._filesystem).hold() as transaction:
            coordinator = PrivateCredentialTransaction(
                self._private,
                self._filesystem.read_authority,
            )
            coordinator.recover()
            self.load(self._snapshot_reader())
            current = self._index.get(account.account_id)
            if (
                current is None
                or (expected is not None and current != expected)
                or current.provider_id is not account.provider_id
                or current.label != account.label
                or current.authority != account.authority
            ):
                raise SourceChangedError
            runtime = dict(self._runtime)
            runtime[account.account_id] = runtime_account_from_saved(
                account,
                runtime[account.account_id].credentials,
            )
            index = AccountIndex(tuple(self._index))
            index.replace(account)
            self._commit_locked(
                transaction,
                coordinator,
                index,
                runtime,
                (),
                source_guard=None,
            )

    def merge_credential_refresh(
        self,
        label: AccountLabel,
        expected_credentials: Credentials,
        update: CredentialRefreshMerge,
    ) -> Account | None:
        """Rebase one refresh result onto freshly reopened v3 state."""
        with self._lock_factory(self._filesystem).hold() as transaction:
            coordinator = PrivateCredentialTransaction(
                self._private,
                self._filesystem.read_authority,
            )
            coordinator.recover()
            self.load(self._snapshot_reader())
            saved = self._saved_for_label(label, provider_id=None)
            if saved is None:
                return None
            current = self._runtime[saved.account_id]
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
                index = AccountIndex(tuple(self._index))
                index.replace(
                    saved_account_from_runtime_state(saved, candidate)
                )
                runtime = dict(self._runtime)
                runtime[saved.account_id] = candidate
                self._commit_locked(
                    transaction,
                    coordinator,
                    index,
                    runtime,
                    (),
                    source_guard=None,
                )
                return copy_runtime_account(candidate)
            index, runtime, bundle = self._candidate_update(saved, candidate)
            bundles = (
                (bundle,) if extra_bundle is None else (bundle, extra_bundle)
            )
            self._commit_locked(
                transaction,
                coordinator,
                index,
                runtime,
                bundles,
                source_guard=None,
            )
            return copy_runtime_account(runtime[saved.account_id])

    def remove(self, label: str) -> bool:
        """Remove one unambiguous account and its legacy authorities."""
        saved = self._saved_for_label(
            AccountLabel(label),
            provider_id=None,
        )
        if saved is None:
            return False
        index = AccountIndex(tuple(self._index))
        index.remove(saved.account_id)
        runtime = dict(self._runtime)
        del runtime[saved.account_id]
        self._commit(index, runtime, ())
        return True

    def rename(self, old: str, new: str) -> bool:
        """Rename one unambiguous account without changing its stable ID."""
        saved = self._saved_for_label(
            AccountLabel(old),
            provider_id=None,
        )
        if saved is None:
            return False
        new_label = AccountLabel(new)
        collision = self._index.resolve(saved.provider_id, new_label)
        if collision is not None and collision.account_id != saved.account_id:
            return False
        if saved.label == new_label:
            return True
        renamed = saved.renamed(new_label)
        index = AccountIndex(tuple(self._index))
        index.replace(renamed)
        runtime = dict(self._runtime)
        runtime[saved.account_id] = copy_runtime_account(
            runtime[saved.account_id],
            label=new_label,
        )
        self._commit(index, runtime, ())
        return True

    def reset_provider(self, provider_id: ProviderId) -> int:
        """Remove every account and legacy authority for one provider."""
        index = AccountIndex(tuple(self._index))
        removed = index.reset_provider(provider_id)
        if not removed:
            return 0
        removed_ids = {account.account_id for account in removed}
        runtime = {
            account_id: account
            for account_id, account in self._runtime.items()
            if account_id not in removed_ids
        }
        self._commit(index, runtime, ())
        return len(removed)

    def recover(self) -> bool:
        """Recover an interrupted credential/index transaction."""
        with self._lock_factory(self._filesystem).hold():
            recovered = PrivateCredentialTransaction(
                self._private,
                self._filesystem.read_authority,
            ).recover()
            if recovered:
                self.load(self._snapshot_reader())
            return recovered

    def generate_label(
        self,
        provider_id: ProviderId,
        plan: str,
    ) -> AccountLabel:
        """Return the smallest unused provider-qualified generated label."""
        plan_component = (plan or "account").lower().replace(" ", "-")
        base = f"{provider_id}-{plan_component}"
        suffix = 1
        while (
            self._index.resolve(
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
            return self._index.resolve(provider_id, label)
        return self._index.resolve_label(label)

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
            target = self._index.resolve(
                account.provider_id,
                account.label,
            )
            if target is not None and target.account_id != previous.account_id:
                raise ValueError("Replacement account label already exists.")
            return previous
        return self._index.resolve(account.provider_id, account.label)

    def _updated_saved(
        self,
        previous: SavedAccount | None,
        account: Account,
    ) -> tuple[SavedAccount, LegacyCredentialAuthority, bytes | None]:
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
        candidate = legacy_saved_account(
            account,
            account_id=account_id,
            authority_id=authority_id,
        )
        candidate = merge_claude_authority(previous, candidate)
        candidate = replace(
            candidate,
            last_refresh_error_code=legacy_error_code(
                account.last_refresh_error
            ),
            last_heartbeat_error_code=legacy_error_code(
                account.last_heartbeat_error
            ),
        )
        authority = authority_for_account(
            account,
            account_id=account_id,
            authority_id=authority_id,
        )
        expected = self._authority_payloads.get((account_id, authority_id))
        return candidate, authority, expected

    def _authority_bundle(
        self,
        authority: LegacyCredentialAuthority,
        expected_payload: bytes | None,
    ) -> PreparedPrivateBundleWrite:
        return self._repository.prepare_write(
            authority,
            expected_payload=expected_payload,
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
        index = AccountIndex(tuple(self._index))
        index.replace(candidate)
        runtime = dict(self._runtime)
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
        with self._lock_factory(self._filesystem).hold() as transaction:
            coordinator = PrivateCredentialTransaction(
                self._private,
                self._filesystem.read_authority,
            )
            coordinator.recover(source_guard=source_guard)
            observed = self._snapshot_reader()
            if not authority_baseline_matches(self._baseline, observed):
                raise SourceChangedError
            self._commit_locked(
                transaction,
                coordinator,
                index,
                runtime,
                bundles,
                source_guard=source_guard,
            )

    def _commit_locked(
        self,
        transaction: _AccountPersistenceTransaction,
        coordinator: PrivateCredentialTransaction,
        index: AccountIndex,
        runtime: dict[SidekickAccountId, Account],
        bundles: tuple[PreparedPrivateBundleWrite, ...],
        *,
        source_guard: CredentialSourceGuard | None,
    ) -> None:
        document = index.document()
        payload = encode_version_three(document)
        validated = decode_version_three(payload)
        introduced, changed, displaced = self._private_changes(
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
        final = coordinator.commit(
            transaction,
            payload,
            self._baseline,
            target_generation=AuthorityGeneration.VERSION_THREE,
            private_bundles=bundles,
            displaced_bundles=displaced,
            source_guard=source_guard,
        )
        if final.data != payload:
            raise DurabilityUncertainError(
                self._filesystem.authority_path.name
            )
        self._adopt_runtime(validated, runtime, final.fingerprint)

    def _private_changes(
        self,
        document: VersionThreeDocument,
        runtime: dict[SidekickAccountId, Account],
        bundles: tuple[PreparedPrivateBundleWrite, ...],
    ) -> tuple[set[Path], set[Path], tuple[Path, ...]]:
        old_authorities = self._authority_paths(tuple(self._index))
        new_authorities = self._authority_paths(document.accounts)
        introduced: set[Path] = new_authorities - old_authorities
        displaced: set[Path] = old_authorities - new_authorities
        changed: set[Path] = {
            bundle.path for bundle in bundles if bundle.path in old_authorities
        }
        old_private = canonical_private_accounts(
            self._runtime.values(),
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
            for authority_id in referenced_legacy_authorities(account)
        }

    def _adopt(
        self,
        document: VersionThreeDocument,
        baseline: ExpectedAuthority,
    ) -> None:
        index = AccountIndex(document.accounts)
        runtime: dict[SidekickAccountId, Account] = {}
        payloads: dict[tuple[SidekickAccountId, AuthorityId], bytes] = {}
        for saved in index:
            for authority_id in referenced_legacy_authorities(saved):
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
            active_id = active_legacy_reference(saved)
            active_payload = payloads.get((saved.account_id, active_id))
            if active_payload is None:
                raise InvalidSchemaError
            active = decode_credential_authority(active_payload)
            require_active_authority_kind(saved, active)
            runtime[saved.account_id] = runtime_account_from_saved(
                saved,
                active.credentials,
            )
        self._index = index
        self._runtime = runtime
        self._authority_payloads = payloads
        self._baseline = baseline

    def _adopt_runtime(
        self,
        document: VersionThreeDocument,
        runtime: dict[SidekickAccountId, Account],
        baseline: ExpectedAuthority,
    ) -> None:
        self._index = AccountIndex(document.accounts)
        self._runtime = {
            account_id: copy_runtime_account(account)
            for account_id, account in runtime.items()
        }
        payloads: dict[tuple[SidekickAccountId, AuthorityId], bytes] = {}
        for saved in self._index:
            for authority_id in referenced_legacy_authorities(saved):
                payload = self._repository.read_payload(
                    saved.account_id,
                    authority_id,
                )
                if payload is None:
                    raise DurabilityUncertainError(
                        self._filesystem.authority_path.name
                    )
                payloads[(saved.account_id, authority_id)] = payload
        self._authority_payloads = payloads
        self._baseline = baseline


__all__ = [
    "AccountLabelAmbiguityError",
    "ManagedAccountStore",
    "ManagedAuthorityResolutionError",
]

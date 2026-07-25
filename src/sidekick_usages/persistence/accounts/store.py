"""Transactional account store over the no-secret account index."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import Protocol, Self

from sidekick_usages.core.accounts.identifiers import (
    new_authority_id,
    new_sidekick_account_id,
)
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import (
    AccountIdFactory,
    AuthorityId,
    AuthorityIdFactory,
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
from sidekick_usages.persistence.accounts.index import (
    AccountIndex,
    safe_error_code,
    saved_account_from_runtime,
)
from sidekick_usages.persistence.accounts.runtime_bridge import (
    CredentialAuthorityUnavailableError,
    active_stored_reference,
    authority_baseline_matches,
    copy_runtime_account,
    credential_authority_reference,
    merge_claude_authority,
    require_active_authority_kind,
    runtime_account_from_saved,
    saved_account_from_runtime_state,
)
from sidekick_usages.persistence.credentials.refresh.merge import (
    CredentialRefreshMerge,
    CredentialRefreshSuccessMerge,
)
from sidekick_usages.persistence.credentials.repository import (
    CredentialAuthorityRepository,
    authority_for_account,
    referenced_stored_authorities,
)
from sidekick_usages.persistence.credentials.transactions.transaction import (
    CredentialSourceGuard,
    PrivateCredentialTransaction,
)
from sidekick_usages.persistence.credentials.transitions.claude import (
    managed_claude_transition_matches,
)
from sidekick_usages.persistence.credentials.transitions.codex import (
    managed_codex_transition_matches,
    stored_codex_transition_matches,
)
from sidekick_usages.persistence.errors import (
    DurabilityUncertainError,
    InvalidSchemaError,
    PrivateCredentialCollisionError,
    SourceChangedError,
)
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.locking import PersistenceLock
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
from sidekick_usages.persistence.types.artifact import AuthorityExpectation

type LockFactory = Callable[
    [PersistenceFilesystem],
    _AccountPersistenceLock,
]
type FilesystemFactory = Callable[[Path], PersistenceFilesystem]


class _AccountPersistenceTransaction(Protocol):
    """Held account-index commit capability."""

    def commit_authority(
        self,
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


class AccountStore:
    """Stable-ID account store for the current schema."""

    def __init__(
        self,
        account_path: Path,
        private_credentials: PrivateCredentialTree,
        *,
        lock_factory: LockFactory = PersistenceLock,
        filesystem_factory: FilesystemFactory = PersistenceFilesystem,
        account_id_factory: AccountIdFactory = new_sidekick_account_id,
        authority_id_factory: AuthorityIdFactory = new_authority_id,
    ) -> None:
        if not account_path.is_absolute():
            raise ValueError("Account authority path must be absolute.")
        self.path = account_path
        self._filesystem = filesystem_factory(account_path)
        self._private = private_credentials
        self._repository = CredentialAuthorityRepository(private_credentials)
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
        self._loaded = False

    def load(self) -> Self:
        """Recover and load the current account index once."""
        if self._loaded:
            return self
        with self._lock_factory(self._filesystem).hold():
            PrivateCredentialTransaction(
                self._private,
                self._filesystem.read_authority,
            ).recover()
            snapshot = self._read_snapshot()
        if snapshot is None:
            self._adopt(
                VersionThreeDocument(()),
                AuthorityExpectation.ABSENT,
            )
        else:
            self._adopt(
                decode_version_three(snapshot.data),
                snapshot.fingerprint,
            )
        self._loaded = True
        return self

    def __iter__(self) -> Iterator[Account]:
        """Iterate defensive runtime account copies."""
        self._require_loaded()
        return iter(
            tuple(
                copy_runtime_account(account)
                for account in self._runtime.values()
            )
        )

    def __len__(self) -> int:
        """Return the managed account count."""
        self._require_loaded()
        return len(self._index)

    def __contains__(self, label: object) -> bool:
        """Return whether any provider owns an exact label."""
        self._require_loaded()
        return any(account.label == label for account in self._index)

    def saved_accounts(
        self,
        provider_id: ProviderId | None = None,
    ) -> tuple[SavedAccount, ...]:
        """Return secret-free accounts in insertion order."""
        self._require_loaded()
        return tuple(
            account
            for account in self._index
            if provider_id is None or account.provider_id is provider_id
        )

    def read_saved(
        self,
        account_id: SidekickAccountId,
    ) -> SavedAccount | None:
        """Reopen and return one secret-free account by stable ID."""
        self._require_loaded()
        self._reload()
        return self._index.get(account_id)

    def resolve_account_id(
        self,
        provider_id: ProviderId,
        label: AccountLabel,
    ) -> SidekickAccountId | None:
        """Resolve one exact provider-qualified label to its stable ID."""
        self._require_loaded()
        account = self._index.resolve(provider_id, label)
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
        runtime = self._runtime.get(account.account_id)
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
        for account in self._runtime.values():
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
            for account in self._runtime.values()
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
        self._require_loaded()
        with self._lock_factory(self._filesystem).hold() as transaction:
            coordinator = PrivateCredentialTransaction(
                self._private,
                self._filesystem.read_authority,
            )
            coordinator.recover()
            self._adopt_snapshot(self._read_snapshot())
            current = self._index.get(account.account_id)
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
            runtime = dict(self._runtime)
            current_runtime = runtime.get(account.account_id)
            if current_runtime is not None:
                runtime[account.account_id] = runtime_account_from_saved(
                    account,
                    current_runtime.credentials,
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

    def migrate_codex_authority(
        self,
        account: SavedAccount,
        *,
        expected: SavedAccount,
    ) -> None:
        """Atomically replace one stored Codex authority with managed state."""
        self._require_loaded()
        with self._lock_factory(self._filesystem).hold() as transaction:
            coordinator = PrivateCredentialTransaction(
                self._private,
                self._filesystem.read_authority,
            )
            coordinator.recover()
            self._adopt_snapshot(self._read_snapshot())
            current = self._index.get(account.account_id)
            if (
                current != expected
                or current is None
                or not stored_codex_transition_matches(current, account)
            ):
                raise SourceChangedError
            index = AccountIndex(tuple(self._index))
            index.replace(account)
            runtime = dict(self._runtime)
            runtime.pop(account.account_id, None)
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
        self._require_loaded()
        with self._lock_factory(self._filesystem).hold() as transaction:
            coordinator = PrivateCredentialTransaction(
                self._private,
                self._filesystem.read_authority,
            )
            coordinator.recover()
            self._adopt_snapshot(self._read_snapshot())
            saved = self._saved_for_label(
                label,
                provider_id=expected_credentials.provider_id,
            )
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
        """Remove one unambiguous account and its stored authorities."""
        self._require_loaded()
        saved = self._saved_for_label(
            AccountLabel(label),
            provider_id=None,
        )
        if saved is None:
            return False
        index = AccountIndex(tuple(self._index))
        index.remove(saved.account_id)
        runtime = dict(self._runtime)
        runtime.pop(saved.account_id, None)
        self._commit(index, runtime, ())
        return True

    def rename(self, old: str, new: str) -> bool:
        """Rename one unambiguous account without changing its stable ID."""
        self._require_loaded()
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
        current_runtime = runtime.get(saved.account_id)
        if current_runtime is not None:
            runtime[saved.account_id] = copy_runtime_account(
                current_runtime,
                label=new_label,
            )
        self._commit(index, runtime, ())
        return True

    def reset_provider(self, provider_id: ProviderId) -> int:
        """Remove every account and stored authority for one provider."""
        self._require_loaded()
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

    def reset_all(self) -> int:
        """Remove every account and its referenced private authorities."""
        self._require_loaded()
        removed = len(self._index)
        if removed:
            self._commit(AccountIndex(), {}, ())
        return removed

    def recover_credentials(self) -> bool:
        """Recover an interrupted credential/index transaction."""
        self._require_loaded()
        with self._lock_factory(self._filesystem).hold():
            recovered = PrivateCredentialTransaction(
                self._private,
                self._filesystem.read_authority,
            ).recover()
            if recovered:
                self._adopt_snapshot(self._read_snapshot())
            return recovered

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
        expected = self._authority_payloads.get((account_id, authority_id))
        return candidate, authority, expected

    def _authority_bundle(
        self,
        authority: StoredCredentialAuthority,
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
            observed = self._read_snapshot()
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
            for authority_id in referenced_stored_authorities(account)
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
        self._authority_payloads = payloads
        self._baseline = baseline

    def _adopt_snapshot(self, snapshot: FileSnapshot | None) -> None:
        """Adopt one current index snapshot or a proven absent state."""
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

    def _read_snapshot(self) -> FileSnapshot | None:
        """Read and validate the sole supported account authority."""
        snapshot = self._filesystem.read_authority()
        if snapshot is not None:
            decode_version_three(snapshot.data)
        return snapshot

    def _reload(self) -> None:
        with self._lock_factory(self._filesystem).hold():
            PrivateCredentialTransaction(
                self._private,
                self._filesystem.read_authority,
            ).recover()
            self._adopt_snapshot(self._read_snapshot())

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise RuntimeError("Account store must be loaded before use.")

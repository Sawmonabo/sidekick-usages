"""Transactional runtime account storage over current schema version two."""

from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol, Self

from sidekick_usages.core.models import Account, Credentials
from sidekick_usages.core.types import (
    AccountLabel,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.paths import AccountLocations
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceObservation,
    assess_persistence,
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
    DuplicateKeyError,
    DurabilityUncertainError,
    FutureSchemaError,
    InvalidSchemaError,
    MalformedJsonError,
    PersistenceCode,
    PersistenceError,
    PrivateCredentialCollisionError,
    SourceChangedError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.inventory import (
    OrphanedPrivateCredentials,
    PersistenceInventory,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.observations import (
    ArtifactKind,
    ArtifactState,
    AuthorityKind,
)
from sidekick_usages.persistence.private_credentials import (
    PreparedPrivateBundleWrite,
    PrivateCredentialOwnership,
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schemas import (
    decode_version_two,
    encode_version_two,
)
from sidekick_usages.persistence.transforms import (
    accounts_to_version_two,
    version_two_to_accounts,
)


class _AccountPersistenceTransaction(Protocol):
    """Lock-scoped operation required by the runtime store."""

    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Commit and prove exact authoritative bytes."""


class _AccountPersistenceLock(Protocol):
    """Cooperative lock yielding one active mutation capability."""

    def hold(
        self,
    ) -> AbstractContextManager[_AccountPersistenceTransaction]:
        """Acquire the persistence lock and yield its capability."""


type _FilesystemFactory = Callable[[Path], PersistenceFilesystem]
type _LockFactory = Callable[
    [PersistenceFilesystem],
    _AccountPersistenceLock,
]
type OrphanedCredentialsObserver = Callable[
    [],
    OrphanedPrivateCredentials,
]


class AccountStoreStateError(PersistenceError):
    """A complete passive assessment blocks runtime store use."""

    def __init__(self, assessment: PersistenceAssessment) -> None:
        """Build a typed error from one frozen safe assessment.

        :param assessment: Blocking passive persistence assessment.
        """
        self.assessment = assessment
        self.code = assessment.code
        self.next_command = assessment.next_command
        super().__init__(assessment.message)


def _copy_account(
    account: Account,
    *,
    label: AccountLabel | None = None,
) -> Account:
    """Return an independently mutable copy of one runtime account."""
    resets = account.heartbeat_window_resets
    return Account(
        label=account.label if label is None else label,
        credentials=account.credentials,
        plan=account.plan,
        last_refresh_at=account.last_refresh_at,
        last_refresh_status=account.last_refresh_status,
        last_refresh_error=account.last_refresh_error,
        heartbeat_enabled=account.heartbeat_enabled,
        heartbeat_5h_reset_at=account.heartbeat_5h_reset_at,
        heartbeat_window_resets=(
            dict(resets.items()) if resets is not None else None
        ),
        heartbeat_targets=account.heartbeat_targets,
        last_heartbeat_at=account.last_heartbeat_at,
        last_heartbeat_status=account.last_heartbeat_status,
        last_heartbeat_error=account.last_heartbeat_error,
    )


def _index_accounts(accounts: tuple[Account, ...]) -> dict[str, Account]:
    """Index validated accounts while preserving their insertion order."""
    return {str(account.label): account for account in accounts}


def _path_text(path: Path) -> str:
    """Return deterministic lexical ordering text for one private path."""
    return str(path)


class AccountStore:
    """Load, query, and transactionally persist current account state."""

    def __init__(
        self,
        locations: AccountLocations,
        *,
        orphaned_credentials_observer: OrphanedCredentialsObserver,
        private_credentials: PrivateCredentialTree | None = None,
        filesystem_factory: _FilesystemFactory = PersistenceFilesystem,
        lock_factory: _LockFactory = PersistenceLock,
    ) -> None:
        """Create an unloaded store bound to one canonical authority.

        :param locations: Discovered account-state locations.
        :param orphaned_credentials_observer: Current private-credential
            evidence provider.
        :param private_credentials: Optional coordinated private-tree owner.
        :param filesystem_factory: Qualified filesystem boundary factory.
        :param lock_factory: Lock-scoped transaction factory.
        """
        self.locations = locations
        self.path = locations.canonical
        self._filesystem = filesystem_factory(self.path)
        self._filesystem_factory = filesystem_factory
        self._lock_factory = lock_factory
        self._orphaned_credentials_observer = orphaned_credentials_observer
        self._private_credentials = private_credentials
        self._inventory = PersistenceInventory(
            self.path,
            locations.prototype_cc_usage,
            filesystem_factory=self._inventory_filesystem,
        )
        self._accounts: dict[str, Account] = {}
        self._baseline: ExpectedAuthority | None = None
        self._loaded = False

    def load(self) -> Self:
        """Load only current schema version two or true absent state.

        :returns: This loaded store.
        """
        if self._loaded:
            return self
        observation, assessment = self._assess()
        if observation.interrupted_credentials:
            if not _private_recovery_is_only_blocker(
                observation,
                assessment,
            ):
                _require_store_assessment(assessment)
            self._recover_private_transaction()
            observation, assessment = self._assess()
        snapshot = self._validated_snapshot(observation, assessment)
        if snapshot is None:
            accounts: dict[str, Account] = {}
            baseline: ExpectedAuthority = AuthorityExpectation.ABSENT
        else:
            document = decode_version_two(snapshot.data)
            accounts = _index_accounts(version_two_to_accounts(document))
            baseline = snapshot.fingerprint
        self._accounts = accounts
        self._baseline = baseline
        self._loaded = True
        return self

    def __iter__(self) -> Iterator[Account]:
        """Iterate over defensive account copies in insertion order."""
        self._require_loaded()
        accounts = tuple(
            _copy_account(account) for account in self._accounts.values()
        )
        return iter(accounts)

    def __len__(self) -> int:
        """Return the loaded account count."""
        self._require_loaded()
        return len(self._accounts)

    def __contains__(self, label: object) -> bool:
        """Return whether the loaded store contains ``label``."""
        self._require_loaded()
        return label in self._accounts

    def get(self, label: str) -> Account | None:
        """Return a defensive copy of one account when present.

        :param label: Exact account label.
        :returns: An independent account or ``None``.
        """
        self._require_loaded()
        account = self._accounts.get(label)
        return _copy_account(account) if account is not None else None

    def read_fresh(self, label: AccountLabel) -> Account | None:
        """Reopen strict durable authority under the normal account lock.

        :param label: Exact account label to return from the reopened state.
        :returns: An independent fresh account or ``None``.
        """
        self._require_loaded()
        with self._lock_factory(self._filesystem).hold():
            expected_source, latest = self._fresh_accounts()
            self._adopt_fresh(latest, expected_source)
            account = latest.get(str(label))
            return _copy_account(account) if account is not None else None

    def find_by_token(
        self,
        provider_id: ProviderId,
        token: str,
    ) -> Account | None:
        """Find an account by provider and exact access token.

        :param provider_id: Provider whose token namespace to search.
        :param token: Exact access token.
        :returns: An independent matching account or ``None``.
        """
        self._require_loaded()
        for account in self._accounts.values():
            if (
                account.provider_id is provider_id
                and account.access_token == token
            ):
                return _copy_account(account)
        return None

    def filter_by_provider(self, provider_id: ProviderId) -> list[Account]:
        """Return independent provider accounts in insertion order.

        :param provider_id: Provider to select.
        :returns: Defensive account copies.
        """
        self._require_loaded()
        return [
            _copy_account(account)
            for account in self._accounts.values()
            if account.provider_id is provider_id
        ]

    def persist(self, account: Account) -> None:
        """Insert or update an account and durably save the store.

        :param account: Complete runtime account to persist.
        """
        self.persist_credentials(account)

    def persist_credentials(
        self,
        account: Account,
        *,
        previous_label: str | None = None,
        private_bundle: PreparedPrivateBundleWrite | None = None,
        source_guard: CredentialSourceGuard | None = None,
    ) -> None:
        """Persist one complete account and optional private bundle.

        :param account: Complete runtime account to persist.
        :param previous_label: Optional old label removed in the same commit.
        :param private_bundle: Optional prepared private credential mutation.
        :param source_guard: Optional retained authority to revalidate.
        """
        self._require_loaded()
        candidate = self._copy_accounts()
        owned_account = _copy_account(account)
        if (
            previous_label is not None
            and previous_label != owned_account.label
        ):
            if previous_label not in candidate:
                raise SourceChangedError
            if str(owned_account.label) in candidate:
                raise ValueError("Replacement account label already exists.")
            del candidate[previous_label]
        candidate[str(owned_account.label)] = owned_account
        bundles = (private_bundle,) if private_bundle is not None else ()
        self._commit_credentials(candidate, bundles, source_guard=source_guard)

    def merge_credential_refresh(
        self,
        label: AccountLabel,
        expected_credentials: Credentials,
        update: CredentialRefreshMerge,
    ) -> Account | None:
        """Rebase and commit only one unchanged refresh target.

        Unrelated accounts and concurrent target metadata are taken from the
        freshly reopened authority while only refresh-owned fields change.
        """
        self._require_loaded()
        with self._lock_factory(self._filesystem).hold() as transaction:
            private = self._private_credentials
            coordinator = (
                PrivateCredentialTransaction(
                    private,
                    self._filesystem.read_authority,
                )
                if private is not None
                else None
            )
            if coordinator is not None:
                coordinator.recover()
            expected_source, latest = self._fresh_accounts()
            current = latest.get(str(label))
            if current is None or current.credentials != expected_credentials:
                self._adopt_fresh(latest, expected_source)
                return _copy_account(current) if current is not None else None
            candidate = _copy_account(current)
            private_bundles: tuple[PreparedPrivateBundleWrite, ...] = ()
            if isinstance(update, CredentialRefreshSuccessMerge):
                candidate.credentials = update.credentials
                if update.plan is not None:
                    candidate.plan = update.plan
                candidate.last_refresh_at = update.completed_at
                candidate.last_refresh_status = RefreshStatus.OK
                candidate.last_refresh_error = None
                if update.private_bundle is not None:
                    private_bundles = (update.private_bundle,)
            else:
                candidate.last_refresh_at = update.completed_at
                candidate.last_refresh_status = RefreshStatus.FAILED
                candidate.last_refresh_error = update.message
            latest[str(label)] = candidate
            payload = encode_version_two(
                accounts_to_version_two(latest.values())
            )
            staged = _index_accounts(
                version_two_to_accounts(decode_version_two(payload))
            )
            final = (
                transaction.commit_authority(
                    AuthorityGeneration.VERSION_TWO,
                    payload,
                    expected_source,
                )
                if coordinator is None
                else coordinator.commit(
                    transaction,
                    payload,
                    expected_source,
                    private_bundles=private_bundles,
                    displaced_bundles=(),
                )
            )
            if final.data != payload:
                raise DurabilityUncertainError(self.path.name)
            self._accounts = staged
            self._baseline = final.fingerprint
            return _copy_account(staged[str(label)])

    def remove(self, label: str) -> bool:
        """Durably remove one account when present.

        :param label: Exact account label.
        :returns: Whether an account was removed.
        """
        return self.remove_credentials(label)

    def remove_credentials(self, label: str) -> bool:
        """Remove one account and only its displaced canonical bundle.

        :param label: Exact account label.
        :returns: Whether an account was removed.
        """
        self._require_loaded()
        if label not in self._accounts:
            return False
        candidate = self._copy_accounts()
        del candidate[label]
        self._commit_credentials(candidate, ())
        return True

    def rename(self, old: str, new: str) -> bool:
        """Durably rename one account while preserving insertion order.

        :param old: Existing exact label.
        :param new: Valid replacement label.
        :returns: Whether the rename was accepted.
        """
        self._require_loaded()
        if old not in self._accounts:
            return False
        new_label = AccountLabel(new)
        if new_label in self._accounts and new_label != old:
            return False
        if new_label == old:
            return True
        candidate: dict[str, Account] = {}
        for label, account in self._accounts.items():
            if label == old:
                candidate[str(new_label)] = _copy_account(
                    account,
                    label=new_label,
                )
            else:
                candidate[label] = _copy_account(account)
        self._commit(candidate)
        return True

    def reset_provider(self, provider_id: ProviderId) -> int:
        """Durably remove every account owned by one provider.

        :param provider_id: Provider whose accounts to remove.
        :returns: Number of removed accounts.
        """
        return self.reset_provider_credentials(provider_id)

    def reset_provider_credentials(self, provider_id: ProviderId) -> int:
        """Remove provider accounts and their unreferenced private bundles.

        :param provider_id: Provider whose accounts to remove.
        :returns: Number of removed accounts.
        """
        self._require_loaded()
        candidate = {
            label: _copy_account(account)
            for label, account in self._accounts.items()
            if account.provider_id is not provider_id
        }
        removed = len(self._accounts) - len(candidate)
        if removed:
            self._commit_credentials(candidate, ())
        return removed

    def recover_credentials(self) -> bool:
        """Resolve one interrupted private transaction under the store lock.

        :returns: Whether recovery evidence was resolved.
        """
        self._require_loaded()
        recovered = self._recover_private_transaction()
        if not recovered:
            return False
        observation, assessment = self._assess()
        snapshot = self._validated_snapshot(observation, assessment)
        if snapshot is None:
            self._accounts = {}
            self._baseline = AuthorityExpectation.ABSENT
        else:
            document = decode_version_two(snapshot.data)
            self._accounts = _index_accounts(version_two_to_accounts(document))
            self._baseline = snapshot.fingerprint
        return True

    def _recover_private_transaction(self) -> bool:
        private = self._require_private_credentials()
        with self._lock_factory(self._filesystem).hold():
            return PrivateCredentialTransaction(
                private,
                self._filesystem.read_authority,
            ).recover()

    def generate_label(
        self,
        provider_id: ProviderId,
        plan: str,
    ) -> AccountLabel:
        """Return the smallest unused provider-plan label.

        :param provider_id: Provider for the label.
        :param plan: Subscription plan label component.
        :returns: A validated unique account label.
        """
        self._require_loaded()
        plan_component = (plan or "account").lower().replace(" ", "-")
        base = f"{provider_id}-{plan_component}"
        suffix = 1
        while f"{base}-{suffix}" in self._accounts:
            suffix += 1
        return AccountLabel(f"{base}-{suffix}")

    def _copy_accounts(self) -> dict[str, Account]:
        return {
            label: _copy_account(account)
            for label, account in self._accounts.items()
        }

    def _fresh_accounts(
        self,
    ) -> tuple[
        ExpectedAuthority,
        dict[str, Account],
    ]:
        observation, assessment = self._assess()
        snapshot = self._validated_snapshot(observation, assessment)
        if snapshot is None:
            return AuthorityExpectation.ABSENT, {}
        document = decode_version_two(snapshot.data)
        return (
            snapshot.fingerprint,
            _index_accounts(version_two_to_accounts(document)),
        )

    def _adopt_fresh(
        self,
        accounts: dict[str, Account],
        baseline: ExpectedAuthority,
    ) -> None:
        self._accounts = {
            label: _copy_account(account)
            for label, account in accounts.items()
        }
        self._baseline = baseline

    def _commit(self, candidate: dict[str, Account]) -> None:
        self._commit_credentials(candidate, ())

    def _commit_credentials(
        self,
        candidate: dict[str, Account],
        private_bundles: tuple[PreparedPrivateBundleWrite, ...],
        *,
        source_guard: CredentialSourceGuard | None = None,
    ) -> None:
        baseline = self._require_loaded()
        document = accounts_to_version_two(candidate.values())
        payload = encode_version_two(document)
        validated = decode_version_two(payload)
        staged = _index_accounts(version_two_to_accounts(validated))
        displaced: tuple[Path, ...] = ()
        private = self._private_credentials
        if private is not None:
            old_private_accounts = _canonical_private_accounts(
                self._accounts.values(),
                private,
            )
            new_private_accounts = _canonical_private_accounts(
                staged.values(),
                private,
            )
            old_references = set(old_private_accounts)
            new_references = set(new_private_accounts)
            prepared_paths: set[Path] = {
                bundle.path for bundle in private_bundles
            }
            if not prepared_paths <= new_references:
                raise ValueError(
                    "Prepared private bundles must be referenced by accounts."
                )
            introduced_paths: set[Path] = new_references.difference(
                old_references,
            )
            if not introduced_paths <= prepared_paths:
                unproven = min(
                    introduced_paths - prepared_paths,
                    key=_path_text,
                )
                raise PrivateCredentialCollisionError(unproven.name)
            changed_paths = {
                path
                for path in old_references & new_references
                if old_private_accounts[path].credentials
                != new_private_accounts[path].credentials
            }
            if not changed_paths <= prepared_paths:
                unproven = min(
                    changed_paths - prepared_paths,
                    key=_path_text,
                )
                raise PrivateCredentialCollisionError(unproven.name)
            removed: set[Path] = old_references.difference(new_references)
            displaced = tuple(sorted(removed, key=_path_text))
        elif private_bundles or source_guard is not None:
            raise RuntimeError(
                "Private credential transaction is not configured."
            )
        with self._lock_factory(self._filesystem).hold() as transaction:
            coordinator = (
                PrivateCredentialTransaction(
                    private,
                    self._filesystem.read_authority,
                )
                if private is not None
                else None
            )
            if coordinator is not None:
                coordinator.recover(source_guard=source_guard)
            observation, assessment = self._assess()
            observed = self._validated_snapshot(observation, assessment)
            if not _baseline_matches(baseline, observed):
                raise SourceChangedError
            final = (
                transaction.commit_authority(
                    AuthorityGeneration.VERSION_TWO,
                    payload,
                    baseline,
                )
                if coordinator is None
                else coordinator.commit(
                    transaction,
                    payload,
                    baseline,
                    private_bundles=private_bundles,
                    displaced_bundles=displaced,
                    source_guard=source_guard,
                )
            )
            if final.data != payload:
                raise DurabilityUncertainError(self.path.name)
            self._accounts = staged
            self._baseline = final.fingerprint

    def _require_private_credentials(self) -> PrivateCredentialTree:
        if self._private_credentials is None:
            raise RuntimeError(
                "Private credential recovery is not configured."
            )
        return self._private_credentials

    def _assess(
        self,
    ) -> tuple[PersistenceObservation, PersistenceAssessment]:
        orphaned = self._orphaned_credentials_observer()
        observation = self._inventory.inspect(orphaned)
        return observation, assess_persistence(observation)

    def _validated_snapshot(
        self,
        observation: PersistenceObservation,
        assessment: PersistenceAssessment,
    ) -> FileSnapshot | None:
        if not _private_recovery_is_only_blocker(observation, assessment):
            _require_store_assessment(assessment)
        snapshot = self._filesystem.read_authority()
        if assessment.code is PersistenceCode.EMPTY:
            if snapshot is not None:
                raise SourceChangedError
            return None
        if snapshot is None or observation.authority.content != snapshot.data:
            raise SourceChangedError
        decode_version_two(snapshot.data)
        return snapshot

    def _inventory_filesystem(self, path: Path) -> PersistenceFilesystem:
        if path == self.path:
            return self._filesystem
        return self._filesystem_factory(path)

    def _require_loaded(self) -> ExpectedAuthority:
        if not self._loaded or self._baseline is None:
            raise RuntimeError("Account store must be loaded before use.")
        return self._baseline


def _require_store_assessment(
    assessment: PersistenceAssessment,
) -> None:
    if assessment.code in {
        PersistenceCode.EMPTY,
        PersistenceCode.CURRENT,
        PersistenceCode.PROTOTYPE_IMPORTED,
    }:
        return
    if assessment.code is PersistenceCode.DUPLICATE_KEY:
        raise DuplicateKeyError
    if assessment.code is PersistenceCode.MALFORMED_JSON:
        raise MalformedJsonError
    if (
        assessment.code is PersistenceCode.FUTURE_SCHEMA
        and assessment.schema_version is not None
    ):
        raise FutureSchemaError(assessment.schema_version)
    if assessment.code is PersistenceCode.INVALID_SCHEMA:
        raise InvalidSchemaError
    raise AccountStoreStateError(assessment)


def _baseline_matches(
    baseline: ExpectedAuthority,
    observed: FileSnapshot | None,
) -> bool:
    if baseline is AuthorityExpectation.ABSENT:
        return observed is None
    return observed is not None and observed.fingerprint == baseline


def _canonical_private_accounts(
    accounts: Iterable[Account],
    private: PrivateCredentialTree,
) -> dict[Path, Account]:
    """Index accounts by their unique canonical private auth home."""
    references: dict[Path, Account] = {}
    for account in accounts:
        auth_home = account.codex_home
        if auth_home is None:
            continue
        path = Path(auth_home)
        if (
            private.classify_bundle(path)
            is PrivateCredentialOwnership.CANONICAL
        ):
            if path in references:
                raise PrivateCredentialCollisionError(path.name)
            references[path] = account
    return references


def _private_recovery_is_only_blocker(
    observation: PersistenceObservation,
    assessment: PersistenceAssessment,
) -> bool:
    """Allow store loading only for one recoverable private journal."""
    if (
        not observation.interrupted_credentials
        or assessment.code is not PersistenceCode.INTERRUPTED_ARTIFACTS
        or observation.authority.kind
        not in {AuthorityKind.ABSENT, AuthorityKind.VERSION_TWO}
    ):
        return False
    if any(
        artifact.kind is ArtifactKind.TEMPORARY
        or artifact.state is not ArtifactState.VALID
        for artifact in observation.artifacts
    ):
        return False
    return all(
        issue.code
        in {
            PersistenceCode.EMPTY,
            PersistenceCode.INTERRUPTED_ARTIFACTS,
            PersistenceCode.CURRENT,
            PersistenceCode.PROTOTYPE_IMPORTED,
        }
        and (
            issue.code is not PersistenceCode.INTERRUPTED_ARTIFACTS
            or issue.artifact_basename is None
        )
        for issue in assessment.issues
    )


__all__ = [
    "AccountStore",
    "AccountStoreStateError",
    "OrphanedCredentialsObserver",
]

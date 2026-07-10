"""Transactional runtime account storage over current schema version one."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol, Self

from sidekick_usages.core.models import Account
from sidekick_usages.core.types import AccountLabel, ProviderId
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
from sidekick_usages.persistence.errors import (
    DuplicateKeyError,
    DurabilityUncertainError,
    FutureSchemaError,
    InvalidSchemaError,
    MalformedJsonError,
    PersistenceCode,
    PersistenceError,
    SourceChangedError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.inventory import (
    OrphanedPrivateCredentials,
    PersistenceInventory,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.schemas import (
    decode_version_one,
    encode_version_one,
)
from sidekick_usages.persistence.transforms import (
    accounts_to_version_one,
    version_one_to_accounts,
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


class AccountStore:
    """Load, query, and transactionally persist current account state."""

    def __init__(
        self,
        locations: AccountLocations,
        *,
        orphaned_credentials_observer: OrphanedCredentialsObserver,
        filesystem_factory: _FilesystemFactory = PersistenceFilesystem,
        lock_factory: _LockFactory = PersistenceLock,
    ) -> None:
        """Create an unloaded store bound to one canonical authority.

        :param locations: Discovered account-state locations.
        :param orphaned_credentials_observer: Current private-credential
            evidence provider.
        :param filesystem_factory: Qualified filesystem boundary factory.
        :param lock_factory: Lock-scoped transaction factory.
        """
        self.locations = locations
        self.path = locations.canonical
        self._filesystem = filesystem_factory(self.path)
        self._filesystem_factory = filesystem_factory
        self._lock_factory = lock_factory
        self._orphaned_credentials_observer = orphaned_credentials_observer
        self._inventory = PersistenceInventory(
            self.path,
            locations.prototype_cc_usage,
            filesystem_factory=self._inventory_filesystem,
        )
        self._accounts: dict[str, Account] = {}
        self._baseline: ExpectedAuthority | None = None
        self._loaded = False

    def load(self) -> Self:
        """Load only current schema version one or true absent state.

        :returns: This loaded store.
        """
        if self._loaded:
            return self
        observation, assessment = self._assess()
        snapshot = self._validated_snapshot(observation, assessment)
        if snapshot is None:
            accounts: dict[str, Account] = {}
            baseline: ExpectedAuthority = AuthorityExpectation.ABSENT
        else:
            document = decode_version_one(snapshot.data)
            accounts = _index_accounts(version_one_to_accounts(document))
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
        self._require_loaded()
        candidate = self._copy_accounts()
        owned_account = _copy_account(account)
        candidate[str(owned_account.label)] = owned_account
        self._commit(candidate)

    def remove(self, label: str) -> bool:
        """Durably remove one account when present.

        :param label: Exact account label.
        :returns: Whether an account was removed.
        """
        self._require_loaded()
        if label not in self._accounts:
            return False
        candidate = self._copy_accounts()
        del candidate[label]
        self._commit(candidate)
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
        self._require_loaded()
        candidate = {
            label: _copy_account(account)
            for label, account in self._accounts.items()
            if account.provider_id is not provider_id
        }
        removed = len(self._accounts) - len(candidate)
        if removed:
            self._commit(candidate)
        return removed

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

    def _commit(self, candidate: dict[str, Account]) -> None:
        baseline = self._require_loaded()
        document = accounts_to_version_one(candidate.values())
        payload = encode_version_one(document)
        validated = decode_version_one(payload)
        staged = _index_accounts(version_one_to_accounts(validated))
        with self._lock_factory(self._filesystem).hold() as transaction:
            observation, assessment = self._assess()
            observed = self._validated_snapshot(observation, assessment)
            if not _baseline_matches(baseline, observed):
                raise SourceChangedError
            final = transaction.commit_authority(
                AuthorityGeneration.VERSION_ONE,
                payload,
                baseline,
            )
            if final.data != payload:
                raise DurabilityUncertainError(self.path.name)
            self._accounts = staged
            self._baseline = final.fingerprint

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
        _require_store_assessment(assessment)
        snapshot = self._filesystem.read_authority()
        if assessment.code is PersistenceCode.EMPTY:
            if snapshot is not None:
                raise SourceChangedError
            return None
        if snapshot is None or observation.authority.content != snapshot.data:
            raise SourceChangedError
        decode_version_one(snapshot.data)
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


__all__ = [
    "AccountStore",
    "AccountStoreStateError",
    "OrphanedCredentialsObserver",
]

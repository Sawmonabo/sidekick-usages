"""Atomic schema-version-two to managed account-index migration."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from sidekick_usages.core.accounts.types import (
    AuthorityId,
    SidekickAccountId,
)
from sidekick_usages.persistence.account_index import AccountIndex
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
)
from sidekick_usages.persistence.credential_transactions import (
    PrivateCredentialTransaction,
)
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.account import VersionThreeDocument
from sidekick_usages.persistence.private_bundle_writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.account import (
    decode_version_three,
    encode_version_three,
)
from sidekick_usages.persistence.schemas import (
    VersionTwoDocument,
    decode_version_two,
)
from sidekick_usages.persistence.transforms import version_two_to_accounts

type AccountIdFactory = Callable[[], SidekickAccountId]
type AuthorityIdFactory = Callable[[], AuthorityId]
type FilesystemFactory = Callable[[Path], PersistenceFilesystem]


class _AuthorityTransaction(Protocol):
    """Held account-authority commit capability."""

    def commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        """Commit exact account authority bytes."""


class _HeldLock(Protocol):
    """Account lock that yields its mutation capability."""

    def hold(self) -> AbstractContextManager[_AuthorityTransaction]:
        """Acquire the account lock."""


type LockFactory = Callable[[PersistenceFilesystem], _HeldLock]


def new_account_id() -> SidekickAccountId:
    """Generate one random stable account ID at the persistence boundary."""
    return SidekickAccountId(str(uuid4()))


def new_authority_id() -> AuthorityId:
    """Generate one random stable authority ID at the persistence boundary."""
    return AuthorityId(str(uuid4()))


@dataclass(frozen=True, slots=True)
class ManagedAccountMigrationPlan:
    """Secret-free target index plus protected authority writes."""

    document: VersionThreeDocument
    authorities: tuple[LegacyCredentialAuthority, ...]
    bundles: tuple[PreparedPrivateBundleWrite, ...]


def plan_version_two_migration(
    source: VersionTwoDocument,
    repository: CredentialAuthorityRepository,
    *,
    account_id_factory: AccountIdFactory,
    authority_id_factory: AuthorityIdFactory,
) -> ManagedAccountMigrationPlan:
    """Build one complete migration candidate without publishing it."""
    index = AccountIndex()
    authorities: list[LegacyCredentialAuthority] = []
    bundles: list[PreparedPrivateBundleWrite] = []
    for account in version_two_to_accounts(source):
        account_id = account_id_factory()
        authority_id = authority_id_factory()
        index.add_legacy(
            account,
            account_id=account_id,
            authority_id=authority_id,
        )
        authority = authority_for_account(
            account,
            account_id=account_id,
            authority_id=authority_id,
        )
        authorities.append(authority)
        bundles.append(repository.prepare_write(authority))
    return ManagedAccountMigrationPlan(
        document=index.document(),
        authorities=tuple(authorities),
        bundles=tuple(bundles),
    )


class ManagedAccountMigrationService:
    """Migrate a current v2 account file and its credentials atomically."""

    def __init__(
        self,
        account_path: Path,
        credential_authorities: PrivateCredentialTree,
        *,
        account_id_factory: AccountIdFactory = new_account_id,
        authority_id_factory: AuthorityIdFactory = new_authority_id,
        filesystem_factory: FilesystemFactory = PersistenceFilesystem,
        lock_factory: LockFactory = PersistenceLock,
    ) -> None:
        if not account_path.is_absolute():
            raise ValueError("Account authority path must be absolute.")
        self._filesystem = filesystem_factory(account_path)
        self._tree = credential_authorities
        self._repository = CredentialAuthorityRepository(
            credential_authorities
        )
        self._account_id_factory = account_id_factory
        self._authority_id_factory = authority_id_factory
        self._lock_factory = lock_factory

    def migrate(self) -> VersionThreeDocument:
        """Publish protected authorities before the no-secret v3 index."""
        with self._lock_factory(self._filesystem).hold() as transaction:
            coordinator = PrivateCredentialTransaction(
                self._tree,
                self._filesystem.read_authority,
            )
            coordinator.recover()
            source = self._filesystem.read_authority()
            if source is None:
                document = VersionThreeDocument(())
                coordinator.commit(
                    transaction,
                    encode_version_three(document),
                    AuthorityExpectation.ABSENT,
                    target_generation=AuthorityGeneration.VERSION_THREE,
                    private_bundles=(),
                    displaced_bundles=(),
                )
                return document
            existing = _try_version_three(source)
            if existing is not None:
                return existing
            legacy = decode_version_two(source.data)
            plan = plan_version_two_migration(
                legacy,
                self._repository,
                account_id_factory=self._account_id_factory,
                authority_id_factory=self._authority_id_factory,
            )
            coordinator.commit(
                transaction,
                encode_version_three(plan.document),
                source.fingerprint,
                target_generation=AuthorityGeneration.VERSION_THREE,
                private_bundles=plan.bundles,
                displaced_bundles=(),
            )
            return plan.document


def _try_version_three(
    snapshot: FileSnapshot,
) -> VersionThreeDocument | None:
    """Return a validated v3 authority, or ``None`` for a valid v2 source."""
    try:
        return decode_version_three(snapshot.data)
    except InvalidSchemaError:
        decode_version_two(snapshot.data)
        return None


__all__ = [
    "AccountIdFactory",
    "AuthorityIdFactory",
    "ManagedAccountMigrationPlan",
    "ManagedAccountMigrationService",
    "new_account_id",
    "new_authority_id",
    "plan_version_two_migration",
]

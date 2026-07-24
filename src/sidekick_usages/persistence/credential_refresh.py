"""Qualified private persistence for saved-credential refresh."""

import hashlib
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sidekick_usages.core.models import Account, Credentials
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.credential_refresh_artifacts import (
    CredentialRefreshActiveError,
    CredentialRefreshArtifacts,
    CredentialRefreshRecoveryBlockedError,
    CredentialRefreshState,
    CredentialRefreshStateKind,
    CredentialRefreshTargetUnavailableError,
    CredentialRefreshUnstableError,
)
from sidekick_usages.persistence.credential_refresh_merge import (
    CredentialRefreshFailureMerge,
    CredentialRefreshSuccessMerge,
)
from sidekick_usages.persistence.errors import (
    DurabilityUncertainError,
    PersistenceError,
    UnsafeManagedFileError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.locking import (
    PersistenceLock,
    StoreLockedError,
)
from sidekick_usages.persistence.models.artifact import FileFingerprint
from sidekick_usages.persistence.private_bundle_writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.refresh import (
    JOURNAL_BASENAME,
    JOURNAL_SCHEMA_VERSION,
    STAGE_BASENAME,
    RefreshJournal,
    RefreshReason,
    account_key_digest,
    credential_digest,
    decode_refresh_journal,
    encode_refresh_journal,
    refresh_credential_kind,
    refresh_reason,
    refresh_timestamp,
    require_sha256,
)
from sidekick_usages.persistence.schema.refresh_stage import (
    decode_credential_refresh_stage,
    encode_credential_refresh_stage,
)
from sidekick_usages.persistence.types.artifact import AuthorityExpectation

__all__ = [
    "CredentialRefreshActiveError",
    "CredentialRefreshArtifacts",
    "CredentialRefreshCrashPoint",
    "CredentialRefreshFaults",
    "CredentialRefreshLease",
    "CredentialRefreshPersistence",
    "CredentialRefreshRecoveryBlockedError",
    "CredentialRefreshState",
    "CredentialRefreshStateKind",
    "CredentialRefreshTargetUnavailableError",
    "CredentialRefreshTransactions",
    "CredentialRefreshUnstableError",
]

_LOCK_DOMAIN = b"sidekick-usages credential refresh lock\0"
_MAX_STABILIZATION_ATTEMPTS = 4


class CredentialRefreshCrashPoint(StrEnum):
    """Durable refresh points available to deterministic fault tests."""

    INTENT_WRITTEN = "intent_written"
    STAGE_WRITTEN = "stage_written"
    STAGE_COMPLETE = "stage_complete"
    ACCOUNT_COMMITTED = "account_committed"
    JOURNAL_COMMITTED = "journal_committed"
    CLEANED = "cleaned"


class CredentialRefreshFaults(Protocol):
    """Observe exact durable points without controlling transaction policy."""

    def reached(self, point: CredentialRefreshCrashPoint) -> None:
        """Observe one exact durable transaction point."""


class _NoCredentialRefreshFaults:
    def reached(self, point: CredentialRefreshCrashPoint) -> None:
        del point


@dataclass(frozen=True, slots=True)
class CredentialRefreshLease:
    """One stable target protected by its refresh-credential hard lock."""

    account: Account = field(repr=False)
    expected_credentials: Credentials = field(repr=False)
    _directory: Path = field(repr=False)
    _journal_fingerprint: FileFingerprint = field(repr=False)


class CredentialRefreshPersistence(Protocol):
    """Private transaction capability consumed by the coordinator."""

    def recover(self) -> None:
        """Resolve all safe local refresh evidence without provider I/O."""

    def hold_lifecycle(self) -> AbstractContextManager[None]:
        """Join the shared refresh-operation lifecycle set."""

    def hold_stable(
        self,
        *,
        provider_id: ProviderId,
        label: AccountLabel,
        reason: str,
        started_at: datetime,
    ) -> AbstractContextManager[CredentialRefreshLease]:
        """Stabilize and hold the lock matching the current credential."""

    def commit_success(
        self,
        lease: CredentialRefreshLease,
        credentials: Credentials,
        plan: str | None,
        completed_at: datetime,
        *,
        private_bundle: PreparedPrivateBundleWrite | None = None,
    ) -> Account | None:
        """Target-merge one validated provider replacement."""

    def prepare_provider_stage(
        self,
        lease: CredentialRefreshLease,
    ) -> Path:
        """Create a qualified child-only home for provider subprocesses."""

    def read_provider_stage(
        self,
        lease: CredentialRefreshLease,
    ) -> bytes | None:
        """Read exact child-produced credentials through held components."""

    def finish_without_exchange(
        self,
        lease: CredentialRefreshLease,
    ) -> Account | None:
        """Clean intent after a concurrent replacement satisfies the call."""

    def persist_failure_if_current(
        self,
        lease: CredentialRefreshLease,
        message: str,
        completed_at: datetime,
    ) -> Account | None:
        """Persist safe failure detail only for an unchanged target."""


class CredentialRefreshTransactions:
    """Own refresh locks, private evidence, merge, and recovery."""

    def __init__(
        self,
        store: AccountStore,
        root: Path,
        *,
        faults: CredentialRefreshFaults | None = None,
    ) -> None:
        """Bind transactions to one store and paths-owned private root."""
        if not root.is_absolute():
            raise ValueError("Credential refresh root must be absolute.")
        self._store = store
        self._root = root
        self._tree = PrivateCredentialTree(root, account_path=store.path)
        self._faults = faults or _NoCredentialRefreshFaults()

    def recover(self) -> None:
        """Resolve every securely inventoried local refresh transaction."""
        try:
            directories = self._tree.list_owned_directories_shallow()
            for directory in directories:
                require_sha256(directory.name)
                evidence_lock = PersistenceLock(
                    PersistenceFilesystem(
                        self._root / _evidence_basename(directory.name)
                    ),
                    timeout_seconds=0,
                )
                try:
                    with evidence_lock.hold():
                        current = self._tree.list_owned_directories_shallow()
                        if directory not in current:
                            continue
                        self._tree.harden_provider_stage(directory)
                        if not self._tree.relative_bundle_present(
                            directory.name
                        ):
                            continue
                        self._recover_directory(directory)
                except StoreLockedError:
                    continue
        except CredentialRefreshRecoveryBlockedError:
            raise
        except PersistenceError, OSError, ValueError:
            raise CredentialRefreshRecoveryBlockedError from None

    @contextmanager
    def hold_lifecycle(self) -> Iterator[None]:
        """Join the shared set excluded by maintenance and full reset."""
        lifecycle = PersistenceLock(
            PersistenceFilesystem(self._root / "lifecycle"),
            shared=True,
        )
        with lifecycle.hold():
            yield

    @contextmanager
    def hold_stable(
        self,
        *,
        provider_id: ProviderId,
        label: AccountLabel,
        reason: str,
        started_at: datetime,
    ) -> Iterator[CredentialRefreshLease]:
        """Hold only a lock proven to match the reloaded target token."""
        validated_reason = refresh_reason(reason)
        latest: Account | None = None
        for _attempt in range(_MAX_STABILIZATION_ATTEMPTS):
            account = self._store.read_fresh(
                label,
                provider_id=provider_id,
            )
            latest = account
            if account is None or account.refresh_token is None:
                raise CredentialRefreshTargetUnavailableError(account)
            expected = account.credentials
            lock = PersistenceLock(
                PersistenceFilesystem(
                    self._root / _operation_basename(account)
                )
            )
            with lock.hold():
                current = self._store.read_fresh(
                    label,
                    provider_id=provider_id,
                )
                latest = current
                if (
                    current is None
                    or current.refresh_token is None
                    or current.credentials != expected
                ):
                    continue
                directory = self._root / account_key_digest(
                    current.provider_id,
                    current.label,
                )
                evidence_lock = PersistenceLock(
                    PersistenceFilesystem(
                        self._root / _evidence_basename(directory.name)
                    )
                )
                with evidence_lock.hold():
                    current = self._store.read_fresh(
                        label,
                        provider_id=provider_id,
                    )
                    latest = current
                    if (
                        current is None
                        or current.refresh_token is None
                        or current.credentials != expected
                    ):
                        continue
                    directory = self._root / account_key_digest(
                        current.provider_id,
                        current.label,
                    )
                    if self._tree.relative_bundle_present(directory.name):
                        raise CredentialRefreshRecoveryBlockedError
                    journal = _intent_journal(
                        current,
                        validated_reason,
                        started_at,
                    )
                    snapshot = self._tree.write_owned_file(
                        directory,
                        JOURNAL_BASENAME,
                        encode_refresh_journal(journal),
                        expected_source=AuthorityExpectation.ABSENT,
                    )
                    self._faults.reached(
                        CredentialRefreshCrashPoint.INTENT_WRITTEN
                    )
                    yield CredentialRefreshLease(
                        current,
                        expected,
                        directory,
                        snapshot.fingerprint,
                    )
                    return
        raise CredentialRefreshUnstableError(latest)

    def commit_success(
        self,
        lease: CredentialRefreshLease,
        credentials: Credentials,
        plan: str | None,
        completed_at: datetime,
        *,
        private_bundle: PreparedPrivateBundleWrite | None = None,
    ) -> Account | None:
        """Stage, target-merge, prove, and clean one provider result."""
        stage_payload = encode_credential_refresh_stage(
            lease.account.label,
            credentials,
            completed_at,
            plan,
            private_bundle,
        )
        self._tree.write_owned_file(
            lease._directory,
            STAGE_BASENAME,
            stage_payload,
            expected_source=AuthorityExpectation.ABSENT,
        )
        self._faults.reached(CredentialRefreshCrashPoint.STAGE_WRITTEN)
        current_journal = self._read_journal(lease._directory)
        if current_journal.stage_state != "intent":
            raise CredentialRefreshRecoveryBlockedError
        complete = current_journal.model_copy(
            update={
                "stage_state": "complete",
                "staged_credential_sha256": credential_digest(credentials),
            }
        )
        journal_snapshot = self._tree.write_owned_file(
            lease._directory,
            JOURNAL_BASENAME,
            encode_refresh_journal(complete),
            expected_source=lease._journal_fingerprint,
        )
        self._faults.reached(CredentialRefreshCrashPoint.STAGE_COMPLETE)
        try:
            committed = self._merge_staged(
                complete,
                lease.account.label,
                credentials,
                completed_at,
                plan,
                private_bundle,
            )
        except DurabilityUncertainError:
            uncertain = complete.model_copy(
                update={"stage_state": "durability_uncertain"}
            )
            self._tree.write_owned_file(
                lease._directory,
                JOURNAL_BASENAME,
                encode_refresh_journal(uncertain),
                expected_source=journal_snapshot.fingerprint,
            )
            raise CredentialRefreshRecoveryBlockedError from None
        self._faults.reached(CredentialRefreshCrashPoint.ACCOUNT_COMMITTED)
        if committed is None:
            self._cleanup(lease._directory)
            return self._store.get(str(lease.account.label))
        committed_journal = complete.model_copy(
            update={"stage_state": "committed"}
        )
        self._tree.write_owned_file(
            lease._directory,
            JOURNAL_BASENAME,
            encode_refresh_journal(committed_journal),
            expected_source=journal_snapshot.fingerprint,
        )
        self._faults.reached(CredentialRefreshCrashPoint.JOURNAL_COMMITTED)
        self._cleanup(lease._directory)
        return committed

    def prepare_provider_stage(
        self,
        lease: CredentialRefreshLease,
    ) -> Path:
        """Create the complete private child-home directory layout."""
        stage_home = lease._directory / "provider-home"
        directories = (
            stage_home,
            stage_home / "AppData",
            stage_home / "AppData" / "Roaming",
            stage_home / "AppData" / "Local",
            stage_home / ".config",
            stage_home / ".claude",
        )
        for directory in directories:
            self._tree.ensure_owned_directory(directory)
        return stage_home

    def read_provider_stage(
        self,
        lease: CredentialRefreshLease,
    ) -> bytes | None:
        """Read exact Claude output through the qualified private tree."""
        self._tree.harden_provider_stage(lease._directory)
        relative = f"{lease._directory.name}/provider-home/.claude"
        snapshot = self._tree.read_relative_bundle_file(
            relative,
            ".credentials.json",
        )
        if snapshot is not None and snapshot.link_count != 1:
            raise UnsafeManagedFileError(".credentials.json")
        return None if snapshot is None else snapshot.data

    def finish_without_exchange(
        self,
        lease: CredentialRefreshLease,
    ) -> Account | None:
        """Clean intent and return the freshly stabilized target."""
        current = self._store.read_fresh(
            lease.account.label,
            provider_id=lease.account.provider_id,
        )
        self._cleanup(lease._directory)
        return current

    def persist_failure_if_current(
        self,
        lease: CredentialRefreshLease,
        message: str,
        completed_at: datetime,
    ) -> Account | None:
        """Persist a failure only while the same credential remains current."""
        candidate = self._store.merge_credential_refresh(
            lease.account.label,
            lease.expected_credentials,
            CredentialRefreshFailureMerge(
                message,
                completed_at,
            ),
        )
        self._cleanup(lease._directory)
        return candidate

    def _recover_directory(self, directory: Path) -> None:
        try:
            require_sha256(directory.name)
        except ValueError:
            raise CredentialRefreshRecoveryBlockedError from None
        journal = self._read_journal(directory)
        if journal.account_key_digest != directory.name:
            raise CredentialRefreshRecoveryBlockedError
        if journal.stage_state == "durability_uncertain":
            raise CredentialRefreshRecoveryBlockedError
        stage_snapshot = self._tree.read_owned_file(
            directory,
            STAGE_BASENAME,
        )
        if stage_snapshot is None:
            if journal.stage_state == "intent":
                self._cleanup(directory)
                return
            raise CredentialRefreshRecoveryBlockedError
        decoded_stage = decode_credential_refresh_stage(stage_snapshot.data)
        plan_update = decoded_stage.plan_update
        private_bundle = decoded_stage.private_bundle
        if (
            account_key_digest(
                decoded_stage.credentials.provider_id,
                decoded_stage.label,
            )
            != journal.account_key_digest
            or decoded_stage.credentials.provider_id.value
            != journal.provider_id
            or refresh_credential_kind(decoded_stage.credentials)
            != journal.expected_credential_kind
        ):
            raise CredentialRefreshRecoveryBlockedError
        staged_digest = credential_digest(decoded_stage.credentials)
        if (
            journal.staged_credential_sha256 is not None
            and journal.staged_credential_sha256 != staged_digest
        ):
            raise CredentialRefreshRecoveryBlockedError
        completed = journal.model_copy(
            update={
                "stage_state": "complete",
                "staged_credential_sha256": staged_digest,
            }
        )
        self._merge_staged(
            completed,
            decoded_stage.label,
            decoded_stage.credentials,
            decoded_stage.completed_at,
            plan_update,
            private_bundle,
        )
        self._cleanup(directory)

    def _merge_staged(
        self,
        journal: RefreshJournal,
        label: AccountLabel,
        credentials: Credentials,
        completed_at: datetime,
        plan_update: str | None,
        private_bundle: PreparedPrivateBundleWrite | None = None,
    ) -> Account | None:
        current = self._store.read_fresh(
            label,
            provider_id=credentials.provider_id,
        )
        if current is None:
            return None
        current_digest = credential_digest(current.credentials)
        staged_digest = credential_digest(credentials)
        if current_digest == staged_digest:
            return current
        if (
            current.provider_id.value != journal.provider_id
            or refresh_credential_kind(current.credentials)
            != journal.expected_credential_kind
            or current_digest != journal.expected_credential_sha256
        ):
            return None
        return self._store.merge_credential_refresh(
            label,
            current.credentials,
            CredentialRefreshSuccessMerge(
                credentials,
                plan_update,
                completed_at,
                private_bundle,
            ),
        )

    def _read_journal(self, directory: Path) -> RefreshJournal:
        snapshot = self._tree.read_owned_file(
            directory,
            JOURNAL_BASENAME,
        )
        if snapshot is None:
            raise CredentialRefreshRecoveryBlockedError
        return decode_refresh_journal(snapshot.data)

    def _cleanup(self, directory: Path) -> None:
        self._tree.harden_provider_stage(directory)
        self._tree.destroy_owned_directory(directory)
        self._faults.reached(CredentialRefreshCrashPoint.CLEANED)


def _operation_basename(account: Account) -> str:
    refresh_token = account.refresh_token
    if refresh_token is None:
        raise ValueError("Refresh operation requires refresh credentials.")
    digest = hashlib.sha256(
        _LOCK_DOMAIN
        + account.provider_id.value.encode("utf-8")
        + b"\0"
        + refresh_token.encode("utf-8")
    ).hexdigest()
    return f"{digest}.refresh"


def _evidence_basename(account_digest: str) -> str:
    require_sha256(account_digest)
    return f"{account_digest}.evidence"


def _intent_journal(
    account: Account,
    reason: RefreshReason,
    started_at: datetime,
) -> RefreshJournal:
    return RefreshJournal(
        schema_version=JOURNAL_SCHEMA_VERSION,
        provider_id=account.provider_id.value,
        account_key_digest=account_key_digest(
            account.provider_id,
            account.label,
        ),
        expected_credential_kind=refresh_credential_kind(account.credentials),
        expected_credential_sha256=credential_digest(account.credentials),
        operation_started_at=refresh_timestamp(started_at),
        refresh_reason=reason,
        stage_state="intent",
        staged_credential_sha256=None,
    )

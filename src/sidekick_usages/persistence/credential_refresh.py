"""Qualified private persistence for saved-credential refresh."""

import hashlib
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sidekick_usages.core.models import Account, Credentials
from sidekick_usages.core.types import (
    AccountLabel,
    RefreshStatus,
)
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    FileFingerprint,
)
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
from sidekick_usages.persistence.credential_refresh_stage import (
    decode_credential_refresh_stage as _decode_stage,
)
from sidekick_usages.persistence.credential_refresh_stage import (
    encode_credential_refresh_stage as _encode_stage,
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
from sidekick_usages.persistence.private_bundle_writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.refresh import (
    JOURNAL_BASENAME as _JOURNAL_BASENAME,
)
from sidekick_usages.persistence.schema.refresh import (
    JOURNAL_SCHEMA_VERSION as _JOURNAL_SCHEMA_VERSION,
)
from sidekick_usages.persistence.schema.refresh import (
    STAGE_BASENAME as _STAGE_BASENAME,
)
from sidekick_usages.persistence.schema.refresh import (
    RefreshJournal as _RefreshJournal,
)
from sidekick_usages.persistence.schema.refresh import (
    RefreshReason as _RefreshReason,
)
from sidekick_usages.persistence.schema.refresh import (
    credential_digest as _credential_digest,
)
from sidekick_usages.persistence.schema.refresh import (
    credential_kind as _credential_kind,
)
from sidekick_usages.persistence.schema.refresh import (
    decode_refresh_journal as _decode_journal,
)
from sidekick_usages.persistence.schema.refresh import (
    encode_refresh_journal as _encode_journal,
)
from sidekick_usages.persistence.schema.refresh import (
    label_digest as _label_digest,
)
from sidekick_usages.persistence.schema.refresh import (
    refresh_reason as _refresh_reason,
)
from sidekick_usages.persistence.schema.refresh import (
    refresh_timestamp as _timestamp,
)
from sidekick_usages.persistence.schema.refresh import (
    require_sha256 as _require_sha256,
)

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
                _require_sha256(directory.name)
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
        """Join the shared set excluded by migration and full reset."""
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
        label: AccountLabel,
        reason: str,
        started_at: datetime,
    ) -> Iterator[CredentialRefreshLease]:
        """Hold only a lock proven to match the reloaded target token."""
        validated_reason = _refresh_reason(reason)
        latest: Account | None = None
        for _attempt in range(_MAX_STABILIZATION_ATTEMPTS):
            account = self._store.read_fresh(label)
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
                current = self._store.read_fresh(label)
                latest = current
                if (
                    current is None
                    or current.refresh_token is None
                    or current.credentials != expected
                ):
                    continue
                directory = self._root / _label_digest(current.label)
                evidence_lock = PersistenceLock(
                    PersistenceFilesystem(
                        self._root / _evidence_basename(directory.name)
                    )
                )
                with evidence_lock.hold():
                    current = self._store.read_fresh(label)
                    latest = current
                    if (
                        current is None
                        or current.refresh_token is None
                        or current.credentials != expected
                    ):
                        continue
                    directory = self._root / _label_digest(current.label)
                    if self._tree.relative_bundle_present(directory.name):
                        raise CredentialRefreshRecoveryBlockedError
                    journal = _intent_journal(
                        current,
                        validated_reason,
                        started_at,
                    )
                    snapshot = self._tree.write_owned_file(
                        directory,
                        _JOURNAL_BASENAME,
                        _encode_journal(journal),
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
        staged = _staged_account(
            lease.account,
            credentials,
            plan,
            completed_at,
        )
        if private_bundle is not None and (
            staged.codex_home is None
            or Path(staged.codex_home) != private_bundle.path
        ):
            raise ValueError(
                "Refresh private bundle does not match its account."
            )
        stage_payload = _encode_stage(staged, plan, private_bundle)
        self._tree.write_owned_file(
            lease._directory,
            _STAGE_BASENAME,
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
                "staged_credential_sha256": _credential_digest(
                    staged.credentials
                ),
            }
        )
        journal_snapshot = self._tree.write_owned_file(
            lease._directory,
            _JOURNAL_BASENAME,
            _encode_journal(complete),
            expected_source=lease._journal_fingerprint,
        )
        self._faults.reached(CredentialRefreshCrashPoint.STAGE_COMPLETE)
        try:
            committed = self._merge_staged(
                complete,
                staged,
                plan,
                private_bundle,
            )
        except DurabilityUncertainError:
            uncertain = complete.model_copy(
                update={"stage_state": "durability_uncertain"}
            )
            self._tree.write_owned_file(
                lease._directory,
                _JOURNAL_BASENAME,
                _encode_journal(uncertain),
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
            _JOURNAL_BASENAME,
            _encode_journal(committed_journal),
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
        current = self._store.read_fresh(lease.account.label)
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
            _require_sha256(directory.name)
        except ValueError:
            raise CredentialRefreshRecoveryBlockedError from None
        journal = self._read_journal(directory)
        if journal.account_label_digest != directory.name:
            raise CredentialRefreshRecoveryBlockedError
        if journal.stage_state == "durability_uncertain":
            raise CredentialRefreshRecoveryBlockedError
        stage_snapshot = self._tree.read_owned_file(
            directory,
            _STAGE_BASENAME,
        )
        if stage_snapshot is None:
            if journal.stage_state == "intent":
                self._cleanup(directory)
                return
            raise CredentialRefreshRecoveryBlockedError
        decoded_stage = _decode_stage(stage_snapshot.data)
        staged = decoded_stage.account
        plan_update = decoded_stage.plan_update
        private_bundle = decoded_stage.private_bundle
        if (
            _label_digest(staged.label) != journal.account_label_digest
            or staged.provider_id.value != journal.provider_id
            or _credential_kind(staged.credentials)
            != journal.expected_credential_kind
        ):
            raise CredentialRefreshRecoveryBlockedError
        staged_digest = _credential_digest(staged.credentials)
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
            staged,
            plan_update,
            private_bundle,
        )
        self._cleanup(directory)

    def _merge_staged(
        self,
        journal: _RefreshJournal,
        staged: Account,
        plan_update: str | None,
        private_bundle: PreparedPrivateBundleWrite | None = None,
    ) -> Account | None:
        current = self._store.read_fresh(staged.label)
        if current is None:
            return None
        current_digest = _credential_digest(current.credentials)
        staged_digest = _credential_digest(staged.credentials)
        if current_digest == staged_digest:
            return current
        if (
            current.provider_id.value != journal.provider_id
            or _credential_kind(current.credentials)
            != journal.expected_credential_kind
            or current_digest != journal.expected_credential_sha256
        ):
            return None
        if staged.last_refresh_at is None:
            raise CredentialRefreshRecoveryBlockedError
        return self._store.merge_credential_refresh(
            staged.label,
            current.credentials,
            CredentialRefreshSuccessMerge(
                staged.credentials,
                plan_update,
                staged.last_refresh_at,
                private_bundle,
            ),
        )

    def _read_journal(self, directory: Path) -> _RefreshJournal:
        snapshot = self._tree.read_owned_file(
            directory,
            _JOURNAL_BASENAME,
        )
        if snapshot is None:
            raise CredentialRefreshRecoveryBlockedError
        return _decode_journal(snapshot.data)

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


def _evidence_basename(label_digest: str) -> str:
    _require_sha256(label_digest)
    return f"{label_digest}.evidence"


def _intent_journal(
    account: Account,
    reason: _RefreshReason,
    started_at: datetime,
) -> _RefreshJournal:
    return _RefreshJournal(
        schema_version=_JOURNAL_SCHEMA_VERSION,
        provider_id=account.provider_id.value,
        account_label_digest=_label_digest(account.label),
        expected_credential_kind=_credential_kind(account.credentials),
        expected_credential_sha256=_credential_digest(account.credentials),
        operation_started_at=_timestamp(started_at),
        refresh_reason=reason,
        stage_state="intent",
        staged_credential_sha256=None,
    )


def _staged_account(
    account: Account,
    credentials: Credentials,
    plan: str | None,
    completed_at: datetime,
) -> Account:
    candidate = _copy_account(account)
    candidate.credentials = credentials
    if plan is not None:
        candidate.plan = plan
    candidate.last_refresh_at = completed_at
    candidate.last_refresh_status = RefreshStatus.OK
    candidate.last_refresh_error = None
    return candidate


def _copy_account(account: Account) -> Account:
    resets = account.heartbeat_window_resets
    return replace(
        account,
        heartbeat_window_resets=(
            dict(resets.items()) if resets is not None else None
        ),
    )


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

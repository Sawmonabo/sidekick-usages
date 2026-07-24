"""Crash-recoverable provider activation journal."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from sidekick_usages.core.accounts.types import (
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    SelectedAccountState,
)
from sidekick_usages.core.selection.policy import (
    decide_activation_recovery,
    transition_activation,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    ActivationRecoveryAction,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.artifact import FileSnapshot
from sidekick_usages.persistence.models.selection import (
    MAX_ACTIVATION_HISTORY,
    ActivationJournalDocument,
)
from sidekick_usages.persistence.private.filesystem import PrivateFilesystem
from sidekick_usages.persistence.schema.selection import (
    decode_activation_journal,
    encode_activation_journal,
)
from sidekick_usages.persistence.state.files import (
    ManagedStateConflictError,
    ManagedStateConflictKind,
    recover_state_file,
)
from sidekick_usages.persistence.state.filesystem import (
    ManagedStateFilesystem,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.persistence.types.activation import StateLockFactory
from sidekick_usages.persistence.types.artifact import AuthorityExpectation

_ACCOUNT_LOCK_DIRECTORY = "account-locks"


def _account_id_key(account_id: SidekickAccountId) -> str:
    """Return the canonical ordering key for account lock acquisition."""
    return str(account_id)


class ActivationJournalTransaction:
    """Provider-and-account locked activation journal capability."""

    def __init__(
        self,
        filesystem: PrivateFilesystem,
        provider_id: ProviderId,
        account_ids: frozenset[SidekickAccountId],
    ) -> None:
        self._filesystem = filesystem
        self.provider_id = provider_id
        self.account_ids = account_ids

    def load(self) -> ActivationJournalDocument:
        """Load the locked journal state."""
        snapshot = self._filesystem.read_opaque_private()
        document = (
            ActivationJournalDocument(provider_id=self.provider_id)
            if snapshot is None
            else decode_activation_journal(snapshot.data)
        )
        if document.provider_id is not self.provider_id:
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        return document

    def begin(self, record: ActivationRecord) -> ActivationRecord:
        """Persist one prepared activation under all required locks."""
        if (
            record.provider_id is not self.provider_id
            or record.phase is not ActivationPhase.PREPARED
            or not self._record_accounts(record) <= self.account_ids
        ):
            raise ValueError("Activation does not match its held locks.")
        snapshot = self._filesystem.read_opaque_private()
        document = (
            ActivationJournalDocument(provider_id=self.provider_id)
            if snapshot is None
            else decode_activation_journal(snapshot.data)
        )
        if document.active == record:
            return record
        if document.active is not None:
            raise ManagedStateConflictError(
                ManagedStateConflictKind.ACTIVE_ACTIVATION
            )
        self._commit(
            ActivationJournalDocument(
                provider_id=self.provider_id,
                active=record,
                history=document.history,
            ),
            snapshot,
        )
        return record

    def advance(
        self,
        operation_id: OperationId,
        phase: ActivationPhase,
        *,
        updated_at: datetime,
        outcome: ActivationOutcome | None = None,
        failure_code: str | None = None,
    ) -> ActivationRecord:
        """Advance the active journal through one legal phase."""
        snapshot = self._filesystem.read_opaque_private()
        document = (
            ActivationJournalDocument(provider_id=self.provider_id)
            if snapshot is None
            else decode_activation_journal(snapshot.data)
        )
        active = document.active
        if (
            active is None
            or active.operation_id != operation_id
            or not self._record_accounts(active) <= self.account_ids
        ):
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        candidate = transition_activation(
            active,
            phase,
            updated_at=updated_at,
            outcome=outcome,
            failure_code=failure_code,
        )
        if candidate.phase.terminal:
            history = (*document.history, candidate)[-MAX_ACTIVATION_HISTORY:]
            updated = ActivationJournalDocument(
                provider_id=self.provider_id,
                history=history,
            )
        else:
            updated = ActivationJournalDocument(
                provider_id=self.provider_id,
                active=candidate,
                history=document.history,
            )
        self._commit(updated, snapshot)
        return candidate

    def commit_verified(
        self,
        operation_id: OperationId,
        state: SelectedAccountState,
        selected: SelectedStateStore,
        *,
        updated_at: datetime,
    ) -> ActivationRecord:
        """Commit verified selection, then close its recoverable journal."""
        document = self.load()
        active = document.active
        if (
            active is None
            or active.operation_id != operation_id
            or active.phase is not ActivationPhase.READ_BACK_VERIFIED
            or state.provider_id is not self.provider_id
            or state.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
            or state.account_id != active.target_account_id
            or state.provider_identity != active.expected_target_identity
        ):
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        selected.save(state)
        return self.advance(
            operation_id,
            ActivationPhase.COMMITTED,
            updated_at=updated_at,
        )

    @staticmethod
    def _record_accounts(
        record: ActivationRecord,
    ) -> frozenset[SidekickAccountId]:
        source = record.source_account_id
        return frozenset(
            {record.target_account_id}
            if source is None
            else {source, record.target_account_id}
        )

    def _commit(
        self,
        document: ActivationJournalDocument,
        snapshot: FileSnapshot | None,
    ) -> None:
        payload = encode_activation_journal(document)
        if snapshot is not None and snapshot.data == payload:
            return
        self._filesystem.commit_opaque_private(
            payload,
            expected_source=(
                AuthorityExpectation.ABSENT
                if snapshot is None
                else snapshot.fingerprint
            ),
        )


class ActivationJournalStore:
    """Own one active journal per provider plus bounded terminal history."""

    def __init__(
        self,
        root: Path,
        *,
        lock_factory: StateLockFactory = PersistenceLock,
    ) -> None:
        if not root.is_absolute():
            raise ValueError("Activation-journal root must be absolute.")
        self.root = root
        self._lock_factory = lock_factory

    def load(self, provider_id: ProviderId) -> ActivationJournalDocument:
        """Load one provider journal without mutation."""
        filesystem = self._provider_filesystem(provider_id)
        snapshot = filesystem.read_opaque_private()
        if snapshot is None:
            return ActivationJournalDocument(provider_id=provider_id)
        document = decode_activation_journal(snapshot.data)
        if document.provider_id is not provider_id:
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        return document

    @contextmanager
    def hold(
        self,
        provider_id: ProviderId,
        account_ids: tuple[SidekickAccountId, ...],
    ) -> Iterator[ActivationJournalTransaction]:
        """Acquire provider first, then stable-ID-sorted account locks."""
        ordered_ids = tuple(
            sorted(
                set(account_ids),
                key=_account_id_key,
            )
        )
        provider_filesystem = self._provider_filesystem(provider_id)
        with ExitStack() as stack:
            provider_transaction = stack.enter_context(
                self._lock_factory(provider_filesystem).hold()
            )
            recover_state_file(provider_filesystem, provider_transaction)
            for account_id in ordered_ids:
                stack.enter_context(
                    self._lock_factory(
                        self._account_lock_filesystem(account_id)
                    ).hold()
                )
            yield ActivationJournalTransaction(
                provider_filesystem,
                provider_id,
                frozenset(ordered_ids),
            )

    def begin(self, record: ActivationRecord) -> ActivationRecord:
        """Persist one prepared activation under its complete lock set."""
        account_ids = self._record_accounts(record)
        with self.hold(record.provider_id, account_ids) as transaction:
            return transaction.begin(record)

    def advance(
        self,
        provider_id: ProviderId,
        operation_id: OperationId,
        phase: ActivationPhase,
        *,
        updated_at: datetime,
        outcome: ActivationOutcome | None = None,
        failure_code: str | None = None,
    ) -> ActivationRecord:
        """Advance an existing activation after locked identity recheck."""
        active = self.load(provider_id).active
        if active is None or active.operation_id != operation_id:
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        with self.hold(
            provider_id,
            self._record_accounts(active),
        ) as transaction:
            return transaction.advance(
                operation_id,
                phase,
                updated_at=updated_at,
                outcome=outcome,
                failure_code=failure_code,
            )

    def recover_from_read_back(
        self,
        read_back: SelectedAccountState,
        selected: SelectedStateStore,
    ) -> ActivationRecoveryAction:
        """Recover one interrupted activation from actual provider state."""
        active = self.load(read_back.provider_id).active
        if active is None:
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        account_ids = list(self._record_accounts(active))
        if read_back.account_id is not None:
            account_ids.append(read_back.account_id)
        with self.hold(
            read_back.provider_id,
            tuple(account_ids),
        ) as transaction:
            current = transaction.load().active
            if current is None or current.operation_id != active.operation_id:
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                )
            action = decide_activation_recovery(current, read_back)
            if action is ActivationRecoveryAction.REQUEST_OFFICIAL_ROLLBACK:
                return action
            if action is ActivationRecoveryAction.COMMIT_VERIFIED:
                current = self._advance_to_read_back(
                    transaction,
                    current,
                    read_back.verified_at,
                )
                state = replace(
                    read_back,
                    runtime_state=ProviderRuntimeState.SAVED_ACTIVE,
                    account_id=current.target_account_id,
                    outcome=ActivationOutcome.VERIFIED,
                )
                transaction.commit_verified(
                    current.operation_id,
                    state,
                    selected,
                    updated_at=read_back.verified_at,
                )
                return action
            state = read_back
            if action is ActivationRecoveryAction.ROLLBACK_VERIFIED:
                state = replace(
                    read_back,
                    outcome=ActivationOutcome.ROLLED_BACK,
                )
            elif action is ActivationRecoveryAction.RECONCILE_EXTERNAL:
                state = replace(
                    read_back,
                    outcome=ActivationOutcome.EXTERNAL_RECONCILED,
                )
            selected.save(state)
            if action is ActivationRecoveryAction.RECONCILIATION_REQUIRED:
                transaction.advance(
                    current.operation_id,
                    ActivationPhase.RECONCILIATION_REQUIRED,
                    updated_at=read_back.verified_at,
                    failure_code="provider_state_untrusted",
                )
                return action
            outcome = (
                ActivationOutcome.EXTERNAL_RECONCILED
                if action is ActivationRecoveryAction.RECONCILE_EXTERNAL
                else (
                    ActivationOutcome.LOGGED_OUT
                    if action is ActivationRecoveryAction.CLOSE_FAILED
                    else ActivationOutcome.ROLLED_BACK
                )
            )
            transaction.advance(
                current.operation_id,
                ActivationPhase.ROLLED_BACK,
                updated_at=read_back.verified_at,
                outcome=outcome,
            )
            return action

    @contextmanager
    def account_removal_guard(
        self,
        account_id: SidekickAccountId,
    ) -> Iterator[None]:
        """Hold provider locks while an owning command removes an account."""
        with ExitStack() as stack:
            documents: list[ActivationJournalDocument] = []
            for provider_id in ProviderId:
                filesystem = self._provider_filesystem(provider_id)
                transaction = stack.enter_context(
                    self._lock_factory(filesystem).hold()
                )
                recover_state_file(filesystem, transaction)
                snapshot = filesystem.read_opaque_private()
                document = (
                    ActivationJournalDocument(provider_id=provider_id)
                    if snapshot is None
                    else decode_activation_journal(snapshot.data)
                )
                if document.provider_id is not provider_id:
                    raise ManagedStateConflictError(
                        ManagedStateConflictKind.CONCURRENT_CHANGE
                    )
                documents.append(document)
            stack.enter_context(
                self._lock_factory(
                    self._account_lock_filesystem(account_id)
                ).hold()
            )
            if any(
                document.active is not None
                and account_id in self._record_accounts(document.active)
                for document in documents
            ):
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.ACTIVE_ACTIVATION
                )
            yield

    def recover(self) -> None:
        """Recover bounded interrupted journal candidates for all providers."""
        for provider_id in ProviderId:
            filesystem = self._provider_filesystem(provider_id)
            with self._lock_factory(filesystem).hold() as transaction:
                recover_state_file(filesystem, transaction)
                self.load(provider_id)

    @staticmethod
    def _advance_to_read_back(
        transaction: ActivationJournalTransaction,
        record: ActivationRecord,
        updated_at: datetime,
    ) -> ActivationRecord:
        if record.phase in {
            ActivationPhase.PREPARED,
            ActivationPhase.OUTGOING_RETAINED,
        }:
            record = transaction.advance(
                record.operation_id,
                ActivationPhase.TARGET_ACTIVATED,
                updated_at=updated_at,
            )
        if record.phase is ActivationPhase.TARGET_ACTIVATED:
            record = transaction.advance(
                record.operation_id,
                ActivationPhase.READ_BACK_VERIFIED,
                updated_at=updated_at,
            )
        if record.phase is not ActivationPhase.READ_BACK_VERIFIED:
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        return record

    @staticmethod
    def _record_accounts(
        record: ActivationRecord,
    ) -> tuple[SidekickAccountId, ...]:
        source = record.source_account_id
        return (
            (record.target_account_id,)
            if source is None
            else (source, record.target_account_id)
        )

    def _provider_filesystem(
        self,
        provider_id: ProviderId,
    ) -> PrivateFilesystem:
        return ManagedStateFilesystem(
            self.root / f"{provider_id.value}.json",
            decode_activation_journal,
        )

    def _account_lock_filesystem(
        self,
        account_id: SidekickAccountId,
    ) -> PrivateFilesystem:
        return PrivateFilesystem(
            self.root / _ACCOUNT_LOCK_DIRECTORY / str(account_id)
        )

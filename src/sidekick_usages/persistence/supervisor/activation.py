"""Crash-recoverable provider activation journal."""

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    ActivationRecord,
    SelectedAccountState,
)
from sidekick_usages.core.selection.policy import (
    same_selected_runtime_authority,
    transition_activation,
)
from sidekick_usages.core.selection.types import (
    ActivationOutcome,
    ActivationPhase,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.locking import (
    LOCK_TIMEOUT_SECONDS,
    PersistenceLock,
)
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
from sidekick_usages.persistence.supervisor.authority import (
    OperationAuthorityLock,
    ProviderMutationAuthority,
    ProviderMutationLock,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.persistence.types.activation import StateLockFactory
from sidekick_usages.persistence.types.artifact import AuthorityExpectation


class ActivationJournalTransaction:
    """Activation journal capability backed by an existing provider lock."""

    def __init__(
        self,
        filesystem: PrivateFilesystem,
        lock_factory: StateLockFactory,
        provider_id: ProviderId,
        account_ids: frozenset[SidekickAccountId],
        provider_authority: ProviderMutationAuthority,
    ) -> None:
        self._filesystem = filesystem
        self._lock_factory = lock_factory
        self._provider_authority = provider_authority
        self.provider_id = provider_id
        self.account_ids = account_ids
        self._require_authority()

    def load(self) -> ActivationJournalDocument:
        """Load the journal under a short-lived journal file lock."""
        with self._hold_document() as (_snapshot, document):
            self._require_active_accounts(document.active)
            return document

    def begin(self, record: ActivationRecord) -> ActivationRecord:
        """Persist one prepared activation under the existing authority."""
        if (
            record.provider_id is not self.provider_id
            or record.phase is not ActivationPhase.PREPARED
            or record.account_ids != self.account_ids
        ):
            raise ValueError("Activation does not match its held authority.")
        with self._hold_document() as (snapshot, document):
            self._require_active_accounts(document.active)
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
        verified_runtime_generation: AuthorityGeneration | None = None,
        outcome: ActivationOutcome | None = None,
        failure_code: str | None = None,
    ) -> ActivationRecord:
        """Advance the active journal under one short file lock."""
        with self._hold_document() as (snapshot, document):
            active = document.active
            if (
                active is None
                or active.operation_id != operation_id
                or active.account_ids != self.account_ids
            ):
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                )
            candidate = transition_activation(
                active,
                phase,
                updated_at=updated_at,
                verified_runtime_generation=verified_runtime_generation,
                outcome=outcome,
                failure_code=failure_code,
            )
            if candidate.phase.terminal:
                history = (*document.history, candidate)[
                    -MAX_ACTIVATION_HISTORY:
                ]
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

    def require_reconciliation(
        self,
        operation_id: OperationId,
        *,
        updated_at: datetime,
        failure_code: str,
    ) -> None:
        """Mark one matching nonterminal activation for reconciliation."""
        active = self.load().active
        if (
            active is None
            or active.operation_id != operation_id
            or active.phase is ActivationPhase.RECONCILIATION_REQUIRED
            or active.phase.terminal
        ):
            return
        self.advance(
            active.operation_id,
            ActivationPhase.RECONCILIATION_REQUIRED,
            updated_at=updated_at,
            verified_runtime_generation=active.verified_runtime_generation,
            failure_code=failure_code,
        )

    def commit_verified(
        self,
        operation_id: OperationId,
        state: SelectedAccountState,
        selected: SelectedStateStore,
        *,
        updated_at: datetime,
    ) -> ActivationRecord:
        """CAS the exact baseline, then close a provider-proven activation."""
        document = self.load()
        active = document.active
        if (
            active is None
            or active.operation_id != operation_id
            or active.phase is not ActivationPhase.PROVIDER_PROOF_VERIFIED
            or state.provider_id is not self.provider_id
            or state.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
            or state.account_id != active.target_account_id
            or state.provider_identity != active.expected_target_identity
            or state.runtime_generation != active.verified_runtime_generation
            or state.outcome is not ActivationOutcome.VERIFIED
        ):
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        current = selected.load(self.provider_id)
        if same_selected_runtime_authority(
            current,
            state,
        ) or _matches_activation_target(current, active):
            expected = current
        else:
            expected = active.selected_baseline
        selected.compare_and_swap(
            state,
            expected=expected,
        )
        return self.advance(
            operation_id,
            ActivationPhase.COMMITTED,
            updated_at=updated_at,
            verified_runtime_generation=state.runtime_generation,
        )

    def commit_rollback(
        self,
        operation_id: OperationId,
        state: SelectedAccountState,
        selected: SelectedStateStore,
        *,
        updated_at: datetime,
    ) -> ActivationRecord:
        """Commit one provider-proven saved source as the rollback."""
        document = self.load()
        active = document.active
        baseline = None if active is None else active.selected_baseline
        if (
            active is None
            or active.operation_id != operation_id
            or baseline is None
            or baseline.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
            or state.provider_id is not self.provider_id
            or state.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
            or state.account_id != baseline.account_id
            or state.provider_identity != baseline.provider_identity
            or state.runtime_generation is None
            or state.outcome is not ActivationOutcome.ROLLED_BACK
        ):
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        current = selected.load(self.provider_id)
        if same_selected_runtime_authority(current, state):
            expected = current
        elif current == baseline:
            expected = baseline
        elif _matches_activation_target(current, active):
            expected = current
        else:
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        selected.compare_and_swap(state, expected=expected)
        return self.advance(
            operation_id,
            ActivationPhase.ROLLED_BACK,
            updated_at=updated_at,
            verified_runtime_generation=state.runtime_generation,
            outcome=ActivationOutcome.ROLLED_BACK,
        )

    def commit_external(
        self,
        operation_id: OperationId,
        state: SelectedAccountState,
        selected: SelectedStateStore,
        *,
        updated_at: datetime,
    ) -> ActivationRecord:
        """Let one proven external choice win and close the journal."""
        document = self.load()
        active = document.active
        baseline = None if active is None else active.selected_baseline
        if (
            active is None
            or active.operation_id != operation_id
            or state.provider_id is not self.provider_id
            or state.runtime_state
            not in {
                ProviderRuntimeState.SAVED_ACTIVE,
                ProviderRuntimeState.EXTERNAL_ACTIVE,
                ProviderRuntimeState.LOGGED_OUT,
                ProviderRuntimeState.UNSUPPORTED,
            }
            or (
                state.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
                and state.outcome is not ActivationOutcome.EXTERNAL_RECONCILED
            )
        ):
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        if state.account_id is not None:
            self._provider_authority.account(state.account_id)
        current = selected.load(self.provider_id)
        if same_selected_runtime_authority(current, state):
            expected = current
        elif current == baseline:
            expected = baseline
        elif _matches_activation_target(current, active):
            expected = current
        else:
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        selected.compare_and_swap(state, expected=expected)
        return self.advance(
            operation_id,
            ActivationPhase.ROLLED_BACK,
            updated_at=updated_at,
            verified_runtime_generation=state.runtime_generation,
            outcome=state.outcome,
        )

    @contextmanager
    def _hold_document(
        self,
    ) -> Iterator[tuple[FileSnapshot | None, ActivationJournalDocument]]:
        self._require_authority()
        with self._lock_factory(self._filesystem).hold() as transaction:
            recover_state_file(self._filesystem, transaction)
            snapshot = self._filesystem.read_opaque_private()
            document = _journal_document(self.provider_id, snapshot)
            yield snapshot, document

    def _require_authority(self) -> None:
        self._provider_authority.require(self.provider_id)
        for account_id in self.account_ids:
            self._provider_authority.account(account_id)

    def _require_active_accounts(
        self,
        record: ActivationRecord | None,
    ) -> None:
        if record is not None and record.account_ids != self.account_ids:
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
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
        operations_root: Path,
        *,
        lock_factory: StateLockFactory = PersistenceLock,
    ) -> None:
        if not root.is_absolute() or not operations_root.is_absolute():
            raise ValueError("Activation paths must be absolute.")
        self.root = root
        self._operations_root = operations_root
        self._lock_factory = lock_factory

    def load(self, provider_id: ProviderId) -> ActivationJournalDocument:
        """Load one provider journal under a short file lock."""
        return self._load_locked(provider_id)

    def observe_all(self) -> tuple[ActivationRecord, ...]:
        """Passively read unfinished activations in provider order."""
        active: list[ActivationRecord] = []
        for provider_id in ProviderId:
            filesystem = self._provider_filesystem(provider_id)
            document = _journal_document(
                provider_id,
                filesystem.read_opaque_private(),
            )
            if document.active is not None:
                active.append(document.active)
        return tuple(active)

    def transaction(
        self,
        provider_id: ProviderId,
        account_ids: tuple[SidekickAccountId, ...],
        provider_authority: ProviderMutationAuthority,
    ) -> ActivationJournalTransaction:
        """Bind journal mutations to an already-held provider authority."""
        ordered_ids = tuple(sorted(set(account_ids)))
        provider_authority.require(provider_id)
        for account_id in ordered_ids:
            provider_authority.account(account_id)
        return ActivationJournalTransaction(
            self._provider_filesystem(provider_id),
            self._lock_factory,
            provider_id,
            frozenset(ordered_ids),
            provider_authority,
        )

    @contextmanager
    def account_removal_guard(
        self,
        account_id: SidekickAccountId,
    ) -> Iterator[None]:
        """Hold provider authorities while the owner removes an account."""
        with ExitStack() as stack:
            for provider_id in ProviderId:
                stack.enter_context(
                    ProviderMutationLock(
                        self._operations_root,
                        provider_id,
                        (),
                        timeout_seconds=LOCK_TIMEOUT_SECONDS,
                    ).hold()
                )
            stack.enter_context(
                OperationAuthorityLock(
                    self._operations_root,
                    account_id,
                ).hold()
            )
            documents = tuple(
                self._load_locked(provider_id) for provider_id in ProviderId
            )
            if any(
                document.active is not None
                and account_id in document.active.account_ids
                for document in documents
            ):
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.ACTIVE_ACTIVATION
                )
            yield

    def recover(self) -> None:
        """Recover bounded interrupted journal candidates for all providers."""
        for provider_id in ProviderId:
            with ProviderMutationLock(
                self._operations_root,
                provider_id,
                (),
                timeout_seconds=LOCK_TIMEOUT_SECONDS,
            ).hold():
                self._recover_locked(provider_id)

    def _load_locked(
        self,
        provider_id: ProviderId,
    ) -> ActivationJournalDocument:
        filesystem = self._provider_filesystem(provider_id)
        with self._lock_factory(filesystem).hold():
            return _journal_document(
                provider_id,
                filesystem.read_opaque_private(),
            )

    def _recover_locked(
        self,
        provider_id: ProviderId,
    ) -> ActivationJournalDocument:
        filesystem = self._provider_filesystem(provider_id)
        with self._lock_factory(filesystem).hold() as transaction:
            recover_state_file(filesystem, transaction)
            return _journal_document(
                provider_id,
                filesystem.read_opaque_private(),
            )

    def _provider_filesystem(
        self,
        provider_id: ProviderId,
    ) -> PrivateFilesystem:
        return ManagedStateFilesystem(
            self.root / f"{provider_id.value}.json",
            decode_activation_journal,
        )


def _journal_document(
    provider_id: ProviderId,
    snapshot: FileSnapshot | None,
) -> ActivationJournalDocument:
    document = (
        ActivationJournalDocument(provider_id=provider_id)
        if snapshot is None
        else decode_activation_journal(snapshot.data)
    )
    if document.provider_id is not provider_id:
        raise ManagedStateConflictError(
            ManagedStateConflictKind.CONCURRENT_CHANGE
        )
    return document


def _matches_activation_target(
    state: SelectedAccountState | None,
    activation: ActivationRecord,
) -> bool:
    return (
        state is not None
        and state.provider_id is activation.provider_id
        and state.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
        and state.account_id == activation.target_account_id
        and state.provider_identity == activation.expected_target_identity
        and state.runtime_generation == activation.verified_runtime_generation
        and state.outcome is ActivationOutcome.VERIFIED
    )

"""Durable provider selection keyed by stable account ID."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path

from sidekick_usages.core.accounts.types import OperationId, SidekickAccountId
from sidekick_usages.core.selection.models import (
    FinalizedSelection,
    OpenSelectionOperation,
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.policy import (
    require_selection_transition,
)
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    SelectionOutcome,
    SelectionPhase,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.filesystem.transaction import (
    PersistenceTransaction,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.artifact import FileSnapshot
from sidekick_usages.persistence.models.selection import (
    MAX_SELECTION_HISTORY,
    SelectedStateDocument,
    SelectionOperationDocument,
)
from sidekick_usages.persistence.schema.selection import (
    decode_selected_state,
    encode_selected_state,
    migrate_selected_state_version_two,
)
from sidekick_usages.persistence.schema.selection_operation import (
    decode_selection_operation,
    encode_selection_operation,
)
from sidekick_usages.persistence.state.files import (
    ManagedStateConflictError,
    ManagedStateConflictKind,
    recover_state_file,
)
from sidekick_usages.persistence.state.filesystem import (
    ManagedStateFilesystem,
)
from sidekick_usages.persistence.supervisor.readers.selection import (
    SelectedStateReader,
)
from sidekick_usages.persistence.types.artifact import (
    AuthorityExpectation,
    sha256_digest,
)


def _selected_state_filesystem(path: Path) -> ManagedStateFilesystem:
    """Build the mutation boundary used by the selected-state store."""
    return ManagedStateFilesystem(
        path,
        decode_selected_state,
    )


def _selection_operation_filesystem(path: Path) -> ManagedStateFilesystem:
    """Build the mutation boundary for one provider operation journal."""
    return ManagedStateFilesystem(
        path,
        decode_selection_operation,
    )


class SelectedStateStore(SelectedStateReader):
    """Persist the last finalized saved selection independently by provider."""

    _filesystem: ManagedStateFilesystem

    def __init__(self, path: Path) -> None:
        super().__init__(
            path,
            filesystem_factory=_selected_state_filesystem,
        )
        self._lock = PersistenceLock(self._filesystem)

    def load(
        self,
        provider_id: ProviderId,
    ) -> FinalizedSelection | None:
        """Load one provider selection, migrating validated v2 when needed."""
        with self._lock.hold() as transaction:
            _snapshot, document = self._load_current(transaction)
            return document.get(provider_id)

    def load_all(self) -> tuple[FinalizedSelection, ...]:
        """Load all selections, migrating validated v2 when needed."""
        with self._lock.hold() as transaction:
            _snapshot, document = self._load_current(transaction)
            return document.states

    def compare_and_swap(
        self,
        state: FinalizedSelection,
        *,
        expected: FinalizedSelection | None,
    ) -> FinalizedSelection:
        """Publish exactly one coordinator-owned forward epoch."""
        if (
            expected is not None
            and state.provider_id is not expected.provider_id
        ):
            raise ValueError("Selected-state providers must match.")
        with self._lock.hold() as transaction:
            snapshot, document = self._load_current(transaction)
            if document.get(state.provider_id) != expected:
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                )
            required_epoch = (
                1 if expected is None else expected.epoch.next().value
            )
            if state.epoch.value != required_epoch:
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                )
            states = {
                current.provider_id: current for current in document.states
            }
            states[state.provider_id] = state
            candidate = SelectedStateDocument(tuple(states.values()))
            payload = encode_selected_state(candidate)
            if snapshot is not None and snapshot.data == payload:
                return state
            self._filesystem.commit_opaque_private(
                payload,
                expected_source=(
                    AuthorityExpectation.ABSENT
                    if snapshot is None
                    else snapshot.fingerprint
                ),
            )
            return state

    def remove_account(self, account_id: SidekickAccountId) -> bool:
        """Remove stale references or reject a currently selected account."""
        with self._lock.hold() as transaction:
            _snapshot, document = self._load_current(transaction)
            if any(
                state.account_id == account_id for state in document.states
            ):
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.SELECTED_ACCOUNT
                )
            return False

    def recover(self) -> None:
        """Discard any bounded interrupted write candidate."""
        with self._lock.hold() as transaction:
            self._load_current(transaction)

    def _load_current(
        self,
        transaction: PersistenceTransaction,
    ) -> tuple[FileSnapshot | None, SelectedStateDocument]:
        recover_state_file(self._filesystem, transaction)
        snapshot = self._filesystem.read_opaque_private()
        if snapshot is None:
            return snapshot, SelectedStateDocument()
        try:
            return snapshot, decode_selected_state(snapshot.data)
        except InvalidSchemaError:
            migrated = migrate_selected_state_version_two(snapshot.data)
        migration_snapshot = self._persist_migration_snapshot(snapshot.data)
        if migration_snapshot.data != snapshot.data:
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        current = self._filesystem.commit_opaque_private(
            encode_selected_state(migrated),
            expected_source=snapshot.fingerprint,
        )
        return current, migrated

    def _persist_migration_snapshot(self, payload: bytes) -> FileSnapshot:
        digest = sha256_digest(payload)
        path = self._filesystem.authority_path.with_name(
            f"{self._filesystem.authority_path.name}.v2.{digest}.bak"
        )
        filesystem = ManagedStateFilesystem(
            path,
            migrate_selected_state_version_two,
        )
        with PersistenceLock(filesystem).hold() as transaction:
            recover_state_file(filesystem, transaction)
            snapshot = filesystem.read_opaque_private()
            if snapshot is None:
                return filesystem.commit_opaque_private(
                    payload,
                    expected_source=AuthorityExpectation.ABSENT,
                )
            migrate_selected_state_version_two(snapshot.data)
            if snapshot.data != payload:
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                )
            return snapshot


class SelectionOperationStore:
    """Own one active global selection and bounded history per provider."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("Selection journal root must be absolute.")
        self.root = root

    def begin(
        self,
        operation: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        """Persist one new provider operation or return its exact replay."""
        if operation.phase is not SelectionPhase.PREVALIDATING:
            raise ValueError("Selection must begin while prevalidating.")
        filesystem = self._provider_filesystem(operation.provider_id)
        with PersistenceLock(filesystem).hold() as transaction:
            recover_state_file(filesystem, transaction)
            snapshot, document = self._document(filesystem)
            if document.active == operation:
                return operation
            if document.active is not None:
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.ACTIVE_ACTIVATION
                )
            self._commit(
                filesystem,
                snapshot,
                SelectionOperationDocument(
                    provider_id=operation.provider_id,
                    active=operation,
                    history=document.history,
                ),
            )
        return operation

    def compare_and_swap(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        """Advance one exact active operation through a forward phase."""
        return self._advance(
            expected,
            replacement,
            merge_required_additions=False,
        )

    def advance_with_required_additions(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        """Advance while atomically preserving late required additions."""
        return self._advance(
            expected,
            replacement,
            merge_required_additions=True,
        )

    def _advance(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
        *,
        merge_required_additions: bool,
    ) -> OpenSelectionOperation:
        """Commit one qualified forward transition under the file lock."""
        filesystem = self._provider_filesystem(expected.provider_id)
        with PersistenceLock(filesystem).hold() as transaction:
            recover_state_file(filesystem, transaction)
            snapshot, document = self._document(filesystem)
            current = document.active
            if current is None:
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                )
            if current != expected and merge_required_additions:
                expected_required = set(expected.required_participant_ids)
                current_required = set(current.required_participant_ids)
                expected_shape = replace(
                    current,
                    required_participant_ids=(
                        expected.required_participant_ids
                    ),
                    updated_at=expected.updated_at,
                )
                if (
                    expected_shape != expected
                    or not expected_required <= current_required
                    or current.updated_at < expected.updated_at
                ):
                    raise ManagedStateConflictError(
                        ManagedStateConflictKind.CONCURRENT_CHANGE
                    )
                additions = current_required - expected_required
                replacement = replace(
                    replacement,
                    required_participant_ids=tuple(
                        sorted(
                            set(replacement.required_participant_ids)
                            | additions
                        )
                    ),
                    updated_at=max(
                        replacement.updated_at,
                        current.updated_at,
                    ),
                )
            elif current != expected:
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                )
            try:
                require_selection_transition(current, replacement)
            except ValueError:
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                ) from None
            self._commit(
                filesystem,
                snapshot,
                SelectionOperationDocument(
                    provider_id=expected.provider_id,
                    active=replacement,
                    history=document.history,
                ),
            )
        return replacement

    def complete(self, result: SelectionResult) -> SelectionResult:
        """Close a proven result or retain ambiguous recovery authority."""
        filesystem = self._provider_filesystem(result.provider_id)
        with PersistenceLock(filesystem).hold() as transaction:
            recover_state_file(filesystem, transaction)
            snapshot, document = self._document(filesystem)
            active = document.active
            if active is None and result in document.history:
                return result
            if active is None or not _selection_result_matches(active, result):
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                )
            if result.outcome is SelectionOutcome.RECOVERY_REQUIRED:
                recovering = replace(
                    active,
                    phase=SelectionPhase.RECOVERING,
                    outcome_code=SelectionCode.SELECTION_RECOVERY_REQUIRED,
                    updated_at=max(active.updated_at, result.completed_at),
                )
                try:
                    require_selection_transition(active, recovering)
                except ValueError:
                    raise ManagedStateConflictError(
                        ManagedStateConflictKind.CONCURRENT_CHANGE
                    ) from None
                self._commit(
                    filesystem,
                    snapshot,
                    SelectionOperationDocument(
                        provider_id=result.provider_id,
                        active=recovering,
                        history=document.history,
                    ),
                )
                return result
            if not _selection_result_may_close(active, result):
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                )
            history = (*document.history, result)[-MAX_SELECTION_HISTORY:]
            self._commit(
                filesystem,
                snapshot,
                SelectionOperationDocument(
                    provider_id=result.provider_id,
                    history=history,
                ),
            )
        return result

    def add_required(
        self,
        provider_id: ProviderId,
        operation_id: OperationId,
        pending_epoch: SelectionEpoch,
        participant_id: ParticipantId,
        *,
        updated_at: datetime,
    ) -> OpenSelectionOperation:
        """Atomically add one late participant to the active operation."""
        filesystem = self._provider_filesystem(provider_id)
        with PersistenceLock(filesystem).hold() as transaction:
            recover_state_file(filesystem, transaction)
            snapshot, document = self._document(filesystem)
            active = document.active
            if (
                active is None
                or active.operation_id != operation_id
                or active.pending_epoch != pending_epoch
            ):
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                )
            if participant_id in active.required_participant_ids:
                return active
            replacement = replace(
                active,
                required_participant_ids=tuple(
                    sorted((*active.required_participant_ids, participant_id))
                ),
                updated_at=max(active.updated_at, updated_at),
            )
            try:
                require_selection_transition(active, replacement)
            except ValueError:
                raise ManagedStateConflictError(
                    ManagedStateConflictKind.CONCURRENT_CHANGE
                ) from None
            self._commit(
                filesystem,
                snapshot,
                SelectionOperationDocument(
                    provider_id=provider_id,
                    active=replacement,
                    history=document.history,
                ),
            )
            return replacement

    def load(
        self,
        provider_id: ProviderId,
    ) -> SelectionOperationDocument:
        """Recover and load one provider's operation journal."""
        filesystem = self._provider_filesystem(provider_id)
        with PersistenceLock(filesystem).hold() as transaction:
            recover_state_file(filesystem, transaction)
            _snapshot, document = self._document(filesystem)
            return document

    def _provider_filesystem(
        self,
        provider_id: ProviderId,
    ) -> ManagedStateFilesystem:
        return _selection_operation_filesystem(
            self.root / f"{provider_id.value}.json"
        )

    @staticmethod
    def _document(
        filesystem: ManagedStateFilesystem,
    ) -> tuple[FileSnapshot | None, SelectionOperationDocument]:
        snapshot = filesystem.read_opaque_private()
        if snapshot is None:
            path = filesystem.authority_path
            provider_id = ProviderId(path.stem)
            return snapshot, SelectionOperationDocument(provider_id)
        document = decode_selection_operation(snapshot.data)
        if document.provider_id.value != filesystem.authority_path.stem:
            raise ManagedStateConflictError(
                ManagedStateConflictKind.CONCURRENT_CHANGE
            )
        return snapshot, document

    @staticmethod
    def _commit(
        filesystem: ManagedStateFilesystem,
        snapshot: FileSnapshot | None,
        document: SelectionOperationDocument,
    ) -> None:
        payload = encode_selection_operation(document)
        if snapshot is not None and snapshot.data == payload:
            return
        filesystem.commit_opaque_private(
            payload,
            expected_source=(
                AuthorityExpectation.ABSENT
                if snapshot is None
                else snapshot.fingerprint
            ),
        )


def _selection_result_matches(
    operation: OpenSelectionOperation | None,
    result: SelectionResult,
) -> bool:
    return (
        operation is not None
        and operation.operation_id == result.operation_id
        and operation.provider_id is result.provider_id
        and operation.target_account_id == result.target_account_id
        and (
            result.outcome is SelectionOutcome.FAILED_OLD_EPOCH
            or operation.target_generation == result.target_generation
        )
        and operation.started_at == result.started_at
        and result.required_count == len(operation.required_participant_ids)
        and result.ready_count == len(operation.ready_participant_ids)
        and result.adopted_count == 0
        and result.lost_count
        == len(operation.lost_after_commit_participant_ids)
        and result.epoch
        == (
            operation.baseline_epoch
            if result.outcome is SelectionOutcome.FAILED_OLD_EPOCH
            else operation.pending_epoch
        )
    )


def _selection_result_may_close(
    operation: OpenSelectionOperation,
    result: SelectionResult,
) -> bool:
    """Return whether an exact active phase may close to this outcome."""
    if result.outcome is SelectionOutcome.FAILED_OLD_EPOCH:
        return operation.phase in {
            SelectionPhase.PREVALIDATING,
            SelectionPhase.PREPARING,
            SelectionPhase.WAITING_OLD_TURNS,
            SelectionPhase.RECOVERING,
        }
    if operation.phase is not SelectionPhase.AWAITING_READY:
        return False
    if result.outcome is SelectionOutcome.READY:
        return operation.outcome_code is None
    return (
        result.outcome is SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT
        and operation.outcome_code
        is SelectionCode.PARTICIPANT_LOST_AFTER_COMMIT
    )

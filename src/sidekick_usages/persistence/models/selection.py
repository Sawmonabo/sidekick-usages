"""Validated models for provider selection and durable operations."""

from dataclasses import dataclass

from sidekick_usages.core.selection.models import (
    ActivationRecord,
    DueOperation,
    FinalizedSelection,
    OpenSelectionOperation,
    SelectionResult,
)
from sidekick_usages.core.selection.types import OperationKind
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.limits import MAX_ACCOUNTS

MAX_ACTIVATION_HISTORY = 32
MAX_SELECTION_HISTORY = 32
MAX_OPERATION_RECORDS = MAX_ACCOUNTS * (len(OperationKind) - 1) + len(
    ProviderId
)


@dataclass(frozen=True, slots=True)
class SelectedStateDocument:
    """Validated finalized selections in deterministic provider order."""

    states: tuple[FinalizedSelection, ...] = ()

    def __post_init__(self) -> None:
        """Reject duplicate providers and normalize ordering."""
        providers = {state.provider_id for state in self.states}
        if len(providers) != len(self.states):
            raise InvalidSchemaError
        object.__setattr__(
            self,
            "states",
            tuple(
                sorted(
                    self.states,
                    key=lambda state: state.provider_id.value,
                )
            ),
        )

    def get(self, provider_id: ProviderId) -> FinalizedSelection | None:
        """Return one provider state when present."""
        return next(
            (
                state
                for state in self.states
                if state.provider_id is provider_id
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class ActivationJournalDocument:
    """One active activation plus bounded terminal history."""

    provider_id: ProviderId
    active: ActivationRecord | None = None
    history: tuple[ActivationRecord, ...] = ()

    def __post_init__(self) -> None:
        """Require provider ownership and active/terminal separation."""
        if len(self.history) > MAX_ACTIVATION_HISTORY:
            raise InvalidSchemaError
        records = (
            self.history
            if self.active is None
            else (*self.history, self.active)
        )
        if any(
            record.provider_id is not self.provider_id for record in records
        ):
            raise InvalidSchemaError
        operation_ids = {record.operation_id for record in records}
        if len(operation_ids) != len(records):
            raise InvalidSchemaError
        if self.active is not None and self.active.phase.terminal:
            raise InvalidSchemaError
        if any(not record.phase.terminal for record in self.history):
            raise InvalidSchemaError


@dataclass(frozen=True, slots=True)
class SelectionOperationDocument:
    """One active global selection plus bounded terminal history."""

    provider_id: ProviderId
    active: OpenSelectionOperation | None = None
    history: tuple[SelectionResult, ...] = ()

    def __post_init__(self) -> None:
        """Require provider ownership and unique operation identities."""
        if len(self.history) > MAX_SELECTION_HISTORY:
            raise InvalidSchemaError
        records = (
            self.history
            if self.active is None
            else (*self.history, self.active)
        )
        if any(
            record.provider_id is not self.provider_id for record in records
        ):
            raise InvalidSchemaError
        operation_ids = {record.operation_id for record in records}
        if len(operation_ids) != len(records):
            raise InvalidSchemaError


@dataclass(frozen=True, slots=True)
class OperationQueueDocument:
    """Validated operation slots in deterministic key order."""

    operations: tuple[DueOperation, ...] = ()

    def __post_init__(self) -> None:
        """Reject duplicate slots or operation identifiers."""
        if len(self.operations) > MAX_OPERATION_RECORDS:
            raise InvalidSchemaError
        slots = {
            operation_queue_slot(operation) for operation in self.operations
        }
        operation_ids = {
            operation.operation_id for operation in self.operations
        }
        if len(slots) != len(self.operations) or len(operation_ids) != len(
            self.operations
        ):
            raise InvalidSchemaError
        object.__setattr__(
            self,
            "operations",
            tuple(
                sorted(
                    self.operations,
                    key=lambda operation: (
                        operation.provider_id.value,
                        str(operation.account_id),
                        operation.kind.value,
                        str(operation.operation_id),
                    ),
                )
            ),
        )


def operation_queue_slot(
    operation: DueOperation,
) -> tuple[str, str, str, str]:
    """Return one ordinary slot or distinct orphan selection tombstone."""
    child_id = (
        str(operation.operation_id)
        if operation.kind.is_selection_worker
        else ""
    )
    return (
        operation.provider_id.value,
        str(operation.account_id),
        operation.kind.value,
        child_id,
    )

"""Structural ports for participant selection coordination."""

import socket
from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from sidekick_usages.core.accounts.types import (
    OperationId,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    FinalizedSelection,
    OpenSelectionOperation,
    PreparedSelection,
    SelectionAuthorityObservation,
    SelectionEpoch,
    SelectionResult,
)
from sidekick_usages.core.selection.types import ParticipantId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionRequest,
    ParticipantConnectionRequest,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantReadyRequest,
    ParticipantRegistration,
    SelectionStatus,
    TurnAdmission,
    TurnBeginRequest,
    TurnEndRequest,
)
from sidekick_usages.persistence.models.selection import (
    SelectionOperationDocument,
)
from sidekick_usages.platform.models import ProcessIdentity


class ParticipantAttachmentTransaction(Protocol):
    """One staged participant attachment with exact ownership transfer."""

    def commit(self) -> None:
        """Commit the staged attachment before membership mutates."""

    def finalize(self) -> None:
        """Release replaced attachment state after membership commits."""

    def rollback(self) -> None:
        """Close or remove the exact staged attachment."""


class ParticipantAttachmentRegistry(Protocol):
    """Provider-owned attachment registry used by the coordinator."""

    def requires_endpoint(self, provider_id: ProviderId) -> bool:
        """Return whether this provider requires a protected endpoint."""

    def requires_finalized_binding(self, provider_id: ProviderId) -> bool:
        """Return whether baseline admission requires attachment binding."""

    def stage(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
        endpoint: socket.socket,
    ) -> ParticipantAttachmentTransaction:
        """Validate and stage one exact endpoint."""

    def remove(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
    ) -> None:
        """Close the endpoint matching one proved dead process."""

    def matches_target(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
        operation_id: OperationId,
        proof: AuthorityReadyProof,
    ) -> bool:
        """Return whether one exact attachment has installed the proof."""

    def matches_finalized(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
        operation_id: OperationId,
        finalized: FinalizedSelection,
    ) -> bool:
        """Return whether one attachment installed the finalized target."""


class SelectionAuthorityAdapter(Protocol):
    """Perform provider work through its qualified worker lane."""

    def prevalidate(
        self,
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
    ) -> PreparedSelection:
        """Prove one target without changing finalized selection."""

    def commit(self, prepared: PreparedSelection) -> AuthorityReadyProof:
        """Commit and prove one provider authority transition."""

    def readback(
        self,
        prepared: PreparedSelection,
    ) -> SelectionAuthorityObservation:
        """Read exact provider state without mutating it."""


@runtime_checkable
class SelectionParticipantBinder(Protocol):
    """Schedule one protected bind for a newly attached participant."""

    def bind_participant(self, operation: OpenSelectionOperation) -> None:
        """Bind the current target before this participant can be ready."""

    def bind_finalized(self, finalized: FinalizedSelection) -> None:
        """Bind persisted target authority before a first participant turn."""


class FinalizedSelectionStore(Protocol):
    """Read and atomically publish provider selection epochs."""

    def load(self, provider_id: ProviderId) -> FinalizedSelection | None:
        """Load one finalized provider selection."""

    def compare_and_swap(
        self,
        state: FinalizedSelection,
        *,
        expected: FinalizedSelection | None,
    ) -> FinalizedSelection:
        """Publish exactly one forward finalized epoch."""


class SelectionJournal(Protocol):
    """Persist one active provider selection operation."""

    def begin(
        self,
        operation: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        """Persist one prevalidating operation."""

    def compare_and_swap(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        """Advance one exact durable operation."""

    def advance_with_required_additions(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
    ) -> OpenSelectionOperation:
        """Advance while preserving only durable required-ID additions."""

    def complete(self, result: SelectionResult) -> SelectionResult:
        """Close or retain one typed operation result."""

    def add_required(
        self,
        provider_id: ProviderId,
        operation_id: OperationId,
        pending_epoch: SelectionEpoch,
        participant_id: ParticipantId,
        *,
        updated_at: datetime,
    ) -> OpenSelectionOperation:
        """Durably add one late participant before acknowledging it."""

    def load(self, provider_id: ProviderId) -> SelectionOperationDocument:
        """Load one provider selection journal."""


class ParticipantControlPort(Protocol):
    """Authenticated participant registration and turn boundary."""

    def register(
        self,
        manifest: ParticipantManifest,
        peer: ProcessIdentity,
        *,
        protected_endpoint: socket.socket | None = None,
    ) -> ParticipantRegistration:
        """Register one kernel-proven participant."""

    def subscribe(
        self,
        request_id: RequestId,
        request: ParticipantConnectionRequest,
        peer: ProcessIdentity,
    ) -> Iterator[ParticipantNotice]:
        """Yield bounded admission notices."""

    def begin_turn(
        self,
        request: TurnBeginRequest,
        peer: ProcessIdentity,
    ) -> TurnAdmission:
        """Admit or queue one exact participant turn."""

    def end_turn(
        self,
        request: TurnEndRequest,
        peer: ProcessIdentity,
    ) -> None:
        """Close one exact participant turn lease."""

    def ready_request(
        self,
        request: ParticipantReadyRequest,
        peer: ProcessIdentity,
    ) -> None:
        """Record one participant readiness request."""

    def adopt_request(
        self,
        request: ParticipantAdoptionRequest,
        peer: ProcessIdentity,
    ) -> None:
        """Record one participant adoption request."""

    def cancel_subscription(
        self,
        request_id: RequestId,
        request: ParticipantConnectionRequest,
        peer: ProcessIdentity,
    ) -> None:
        """Cancel and disconnect one exact participant stream."""


class SelectionControlPort(Protocol):
    """Provider-neutral account selection and status boundary."""

    def select_events(
        self,
        operation_id: OperationId,
        provider_id: ProviderId,
        target_account_id: SidekickAccountId,
    ) -> tuple[
        OperationId,
        Iterator[SelectionStatus | SelectionResult],
    ]:
        """Open one canonical flight and return its phase stream."""

    def select(
        self,
        operation_id: OperationId,
        provider_id: ProviderId,
        target_account_id: SidekickAccountId,
    ) -> SelectionResult:
        """Select one saved target or return a typed result."""

    def status(self, provider_id: ProviderId) -> SelectionStatus:
        """Return one provider selection status snapshot."""


class SelectionSupervisorPort(
    ParticipantControlPort,
    SelectionControlPort,
    Protocol,
):
    """Complete authenticated selection surface owned by the supervisor."""

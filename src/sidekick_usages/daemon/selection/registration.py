"""Exact participant registration and protected binding admission."""

import socket
from collections.abc import Callable
from threading import Lock

from sidekick_usages.clock import Clock
from sidekick_usages.core.accounts.types import OperationId
from sidekick_usages.core.selection.models import SelectionEpoch
from sidekick_usages.core.selection.types import SelectionCode
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.selection.models import (
    ParticipantManifest,
    ParticipantRegistration,
    ParticipantRequestError,
)
from sidekick_usages.daemon.selection.ports import (
    FinalizedSelectionStore,
    ParticipantAttachmentRegistry,
    SelectionAuthorityAdapter,
    SelectionJournal,
    SelectionParticipantBinder,
)
from sidekick_usages.daemon.selection.registry import ParticipantRegistry
from sidekick_usages.platform.models import ProcessIdentity

_AUTHORITY_PROOF_FAILED = SelectionCode.AUTHORITY_PROOF_FAILED


class ParticipantRegistrar:
    """Register one participant and admit its exact protected binding."""

    def __init__(
        self,
        selected: FinalizedSelectionStore,
        journal: SelectionJournal,
        participants: ParticipantRegistry,
        adapter: SelectionAuthorityAdapter,
        clock: Clock,
        resume_recovery: Callable[[ProviderId], None],
    ) -> None:
        self._selected = selected
        self._journal = journal
        self._participants = participants
        self._adapter = adapter
        self._clock = clock
        self._resume_recovery = resume_recovery
        self._lock = Lock()

    def register(
        self,
        manifest: ParticipantManifest,
        peer: ProcessIdentity,
        protected_endpoint: socket.socket | None,
    ) -> ParticipantRegistration:
        """Durably register and bind one kernel-proven participant."""

        def persist_required(pending_epoch: SelectionEpoch) -> None:
            active = self._journal.load(manifest.provider_id).active
            if active is None or active.pending_epoch != pending_epoch:
                raise RuntimeError("selection_journal_unavailable")
            self._journal.add_required(
                manifest.provider_id,
                active.operation_id,
                active.pending_epoch,
                manifest.participant_id,
                updated_at=self._clock.now(),
            )

        owner = self._participants.attachment_registry(manifest.provider_id)
        reported_id: OperationId | None = None
        if owner is None:
            if protected_endpoint is not None:
                protected_endpoint.close()
                raise ParticipantRequestError(_AUTHORITY_PROOF_FAILED)
            registration = self._participants.register(
                manifest,
                peer,
                persist_required=persist_required,
            )
        else:
            if protected_endpoint is None:
                raise ParticipantRequestError(_AUTHORITY_PROOF_FAILED)
            registration = self._register_attached(
                manifest,
                peer,
                protected_endpoint,
                persist_required,
            )
            try:
                unbound, reported_id = owner.refresh_binding(
                    manifest.participant_id,
                    manifest.connection_generation,
                    peer,
                )
                self._participants.record_prebootstrap_proof(
                    manifest.participant_id,
                    manifest.connection_generation,
                    peer,
                    unbound,
                )
            except Exception:
                raise ParticipantRequestError(
                    _AUTHORITY_PROOF_FAILED
                ) from None
        self._bind(registration, peer, owner, reported_id)
        return registration

    def _register_attached(
        self,
        manifest: ParticipantManifest,
        peer: ProcessIdentity,
        endpoint: socket.socket,
        persist_required: Callable[[SelectionEpoch], None],
    ) -> ParticipantRegistration:
        with self._lock:
            try:
                transaction = self._participants.stage_attachment(
                    manifest, peer, endpoint
                )
            except Exception:
                raise ParticipantRequestError(
                    _AUTHORITY_PROOF_FAILED
                ) from None
            try:
                return self._participants.register(
                    manifest,
                    peer,
                    persist_required=persist_required,
                    attachment=transaction,
                )
            except BaseException:
                transaction.rollback()
                raise

    def _bind(
        self,
        registration: ParticipantRegistration,
        peer: ProcessIdentity,
        owner: ParticipantAttachmentRegistry | None,
        reported_id: OperationId | None,
    ) -> None:
        provider_id = registration.provider_id
        binder = self._adapter
        if registration.pending_epoch is not None:
            active = self._journal.load(provider_id).active
            if (
                active is not None
                and active.target_generation is not None
                and isinstance(binder, SelectionParticipantBinder)
            ):
                binder.bind_participant(
                    active,
                    registration.participant_id,
                    registration.connection_generation,
                )
            self._resume_recovery(provider_id)
            return
        if not self._participants.requires_finalized_attachment(
            provider_id
        ) or not isinstance(binder, SelectionParticipantBinder):
            return
        finalized = self._selected.load(provider_id)
        if finalized is None:
            if reported_id is not None:
                self._reject_binding(registration, peer, owner)
            return
        if reported_id is None:
            binder.bind_finalized(
                finalized,
                registration.participant_id,
                registration.connection_generation,
            )
            return
        if owner is not None and owner.matches_finalized(
            registration.participant_id,
            registration.connection_generation,
            peer,
            reported_id,
            finalized,
        ):
            self._participants.prepare_finalized(
                reported_id,
                finalized,
                registration.participant_id,
            )
            return
        self._reject_binding(registration, peer, owner)

    def _reject_binding(
        self,
        registration: ParticipantRegistration,
        peer: ProcessIdentity,
        owner: ParticipantAttachmentRegistry | None,
    ) -> None:
        if owner is not None:
            owner.remove(
                registration.participant_id,
                registration.connection_generation,
                peer,
            )
        self._participants.disconnect(
            registration.participant_id,
            registration.connection_generation,
            attachment_failure=True,
        )
        raise ParticipantRequestError(_AUTHORITY_PROOF_FAILED)

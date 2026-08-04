"""Protected local channels for resident Claude participants."""

import os
import socket
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Protocol

from sidekick_usages.core.accounts.identifiers import new_request_id
from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    OperationId,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    FinalizedSelection,
    OpenSelectionOperation,
    SelectionAuthorityObservation,
    SelectionEpoch,
    SelectionRecoveryDecision,
)
from sidekick_usages.core.selection.policy import selection_recovery_decision
from sidekick_usages.core.selection.types import (
    OperationKind,
    ParticipantId,
    SelectionCode,
    SelectionRecoveryRelation,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.platform.models import ProcessIdentity
from sidekick_usages.providers.claude.structured.codec import (
    MAX_CLAUDE_PROTECTED_FRAME_BYTES,
    ClaudeProtectedChannelClosedError,
    ClaudeProtectedChannelError,
    ClaudeProtectedExchangeInstruction,
    clear_secret_buffer,
    decode_protected_exchange_instruction,
    decode_protected_projection,
    encode_protected_ack,
    encode_protected_binding_query,
    encode_protected_binding_report,
    encode_protected_exchange_instruction,
    encode_protected_install_receipt,
    encode_protected_projection,
    require_protected_ack,
    require_protected_binding_query,
    require_protected_binding_report,
    require_protected_install_receipt,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredBinding,
    ClaudeStructuredInstallReceipt,
)
from sidekick_usages.providers.claude.structured.protected_frame import (
    ClaudeProtectedOAuthFrame,
)
from sidekick_usages.providers.claude.structured.transport import (
    receive_protected_socket_frame,
)
from sidekick_usages.serialization.framing import (
    clear_mutable_buffer,
    encode_bounded_frame,
)

MAX_CLAUDE_PARTICIPANT_CHANNELS = 16
CLAUDE_PROTECTED_RESPONSE_SECONDS = 8.0
CLAUDE_PROTECTED_COMPLETION_SECONDS = 120.0
_CLAUDE_PROJECTION_KINDS = frozenset(
    {
        OperationKind.SELECTION_COMMIT,
        OperationKind.CLAUDE_PARTICIPANT_BIND,
    }
)


def claude_participant_ack_required(
    accounts: Callable[[], tuple[SavedAccount, ...]],
    account_id: SidekickAccountId,
) -> bool:
    """Return whether one exact Claude target needs an integrated host."""
    matches = tuple(
        account
        for account in accounts()
        if account.account_id == account_id
        and account.provider_id is ProviderId.CLAUDE
    )
    if len(matches) != 1:
        raise ClaudeProtectedChannelError(
            "The protected Claude target is unavailable."
        )
    return not matches[0].has_managed_authority


@dataclass(slots=True)
class _ClaudeParticipantChannel:
    """One exact endpoint and its kernel-proven participant binding."""

    endpoint: socket.socket
    connection_generation: int
    peer: ProcessIdentity
    binding: ClaudeStructuredBinding | None = None


class ClaudeParticipantChannelTransaction:
    """Transfer one staged endpoint to its registry exactly once."""

    __slots__ = (
        "_closed",
        "_committed",
        "_endpoint",
        "_generation",
        "_participant_id",
        "_peer",
        "_registry",
        "_replaced",
    )

    def __init__(
        self,
        registry: ClaudeParticipantChannelRegistry,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
        endpoint: socket.socket,
    ) -> None:
        self._registry = registry
        self._participant_id = participant_id
        self._generation = connection_generation
        self._peer = peer
        self._endpoint: socket.socket | None = endpoint
        self._closed = False
        self._committed = False
        self._replaced: _ClaudeParticipantChannel | None = None

    def commit(self) -> None:
        """Commit the staged endpoint, replacing only an older generation."""
        endpoint = self._take_endpoint()
        try:
            self._replaced = self._registry._commit(
                self._participant_id,
                self._generation,
                self._peer,
                endpoint,
            )
            self._committed = True
        except BaseException:
            endpoint.close()
            raise

    def finalize(self) -> None:
        """Close one replaced endpoint after membership commits."""
        replaced = self._replaced
        self._replaced = None
        self._committed = False
        if replaced is not None:
            replaced.endpoint.close()

    def rollback(self) -> None:
        """Close one uncommitted endpoint without changing the registry."""
        endpoint = self._endpoint
        self._endpoint = None
        self._closed = True
        replaced = self._replaced
        self._replaced = None
        if self._committed:
            endpoint = self._registry._rollback(
                self._participant_id,
                self._generation,
                self._peer,
                replaced,
            )
            self._committed = False
        if endpoint is not None:
            endpoint.close()

    def _take_endpoint(self) -> socket.socket:
        endpoint = self._endpoint
        if self._closed or endpoint is None:
            raise ClaudeProtectedChannelError(
                "The protected channel transaction is closed."
            )
        self._endpoint = None
        self._closed = True
        return endpoint

    def __repr__(self) -> str:
        """Return only secret-free transaction state."""
        state = "closed" if self._closed else "staged"
        return f"<ClaudeParticipantChannelTransaction {state}>"


class ClaudeParticipantChannelRegistry:
    """Own bounded process-bound participant endpoints without a listener."""

    def __init__(
        self,
        participant_required: Callable[[SidekickAccountId], bool],
        participant_failed: Callable[[ParticipantId, int], None] | None = None,
    ) -> None:
        self._lock = Lock()
        self._distribution_lock = Lock()
        self._channels: dict[ParticipantId, _ClaudeParticipantChannel] = {}
        self._participant_required = participant_required
        self._participant_failed = participant_failed

    @staticmethod
    def requires_endpoint(provider_id: ProviderId) -> bool:
        """Require protected endpoints only for Claude participants."""
        return provider_id is ProviderId.CLAUDE

    @staticmethod
    def requires_finalized_binding(provider_id: ProviderId) -> bool:
        """Require Claude authority installation before baseline admission."""
        return provider_id is ProviderId.CLAUDE

    def requires_participant(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
    ) -> bool:
        """Return whether this target requires one protected participant."""
        if provider_id is not ProviderId.CLAUDE:
            raise ClaudeProtectedChannelError(
                "The protected participant provider does not match."
            )
        return self._participant_required(account_id)

    def recovery_decision(
        self,
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
        observation: SelectionAuthorityObservation,
        *,
        target_binding_proven: bool,
    ) -> SelectionRecoveryDecision:
        """Relate Claude mode, native truth, and exact host binding."""
        classified = observation.authority_requires_participant
        required = self.requires_participant(
            operation.provider_id, operation.target_account_id
        )
        if classified is None or classified != required:
            return SelectionRecoveryDecision(
                relation=SelectionRecoveryRelation.UNRESOLVED,
                target_generation=None,
                safe_code=SelectionCode.SELECTION_RECOVERY_REQUIRED,
            )
        return selection_recovery_decision(
            operation,
            baseline,
            observation,
            target_binding_proven=target_binding_proven,
            baseline_observation_conclusive=not classified,
        )

    def stage(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
        endpoint: socket.socket,
    ) -> ClaudeParticipantChannelTransaction:
        """Validate one endpoint before the membership transaction commits."""
        try:
            self._require_endpoint(endpoint)
            with self._lock:
                current = self._channels.get(participant_id)
                if current is not None and (
                    connection_generation <= current.connection_generation
                ):
                    raise ClaudeProtectedChannelError(
                        "The protected participant binding was replayed."
                    )
                if (
                    current is None
                    and len(self._channels) >= MAX_CLAUDE_PARTICIPANT_CHANNELS
                ):
                    raise ClaudeProtectedChannelError(
                        "The protected participant capacity was reached."
                    )
            os.set_inheritable(endpoint.fileno(), False)
        except BaseException:
            endpoint.close()
            raise
        return ClaudeParticipantChannelTransaction(
            self,
            participant_id,
            connection_generation,
            peer,
            endpoint,
        )

    def remove(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
    ) -> None:
        """Close only the endpoint matching one proved old connection."""
        endpoint: socket.socket | None = None
        with self._lock:
            current = self._channels.get(participant_id)
            if current is None:
                return
            if (
                current.connection_generation != connection_generation
                or current.peer != peer
            ):
                raise ClaudeProtectedChannelError(
                    "The protected participant binding does not match."
                )
            endpoint = self._channels.pop(participant_id).endpoint
        endpoint.close()

    def matches_target(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
        operation_id: OperationId,
        proof: AuthorityReadyProof,
    ) -> bool:
        """Return whether an exact channel acknowledged the target proof."""
        with self._lock:
            current = self._channels.get(participant_id)
            return current is not None and (
                current.connection_generation == connection_generation
                and current.peer == peer
                and current.binding
                == ClaudeStructuredBinding(
                    operation_id=operation_id,
                    account_id=proof.account_id,
                    generation=proof.generation,
                    epoch=proof.epoch,
                )
            )

    def matches_finalized(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
        operation_id: OperationId,
        finalized: FinalizedSelection,
    ) -> bool:
        """Return whether an exact channel acknowledged finalized authority."""
        return self.matches_target(
            participant_id,
            connection_generation,
            peer,
            operation_id,
            AuthorityReadyProof(
                provider_id=finalized.provider_id,
                account_id=finalized.account_id,
                generation=finalized.generation,
                epoch=finalized.epoch,
                safe_code=SelectionCode.SELECTION_SUCCEEDED,
            ),
        )

    def refresh_binding(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
    ) -> bool:
        """Refresh and report whether one exact channel is unbound."""
        with self._distribution_lock:
            with self._lock:
                channel = self._channels.get(participant_id)
                if channel is None or (
                    channel.connection_generation != connection_generation
                    or channel.peer != peer
                ):
                    raise ClaudeProtectedChannelError(
                        "The protected participant binding does not match."
                    )
            nonce = new_request_id()
            payload = encode_protected_binding_query(
                nonce, participant_id, connection_generation
            )
            frame = encode_bounded_frame(
                payload, MAX_CLAUDE_PROTECTED_FRAME_BYTES
            )
            try:
                channel.endpoint.settimeout(CLAUDE_PROTECTED_RESPONSE_SECONDS)
                channel.endpoint.sendall(frame)
                report = require_protected_binding_report(
                    receive_protected_socket_frame(channel.endpoint),
                    nonce,
                    participant_id,
                    connection_generation,
                )
                with self._lock:
                    current = self._channels.get(participant_id)
                    if current is not channel:
                        raise ClaudeProtectedChannelError(
                            "The participant reconnected during query."
                        )
                    current.binding = report
                return report is None
            except ClaudeProtectedChannelError, OSError, ValueError:
                self._discard_failed(participant_id, channel)
                raise ClaudeProtectedChannelError(
                    "The protected Claude binding was not reported."
                ) from None
            finally:
                clear_mutable_buffer(frame)

    def close(self) -> None:
        """Close every endpoint owned by this supervisor registry."""
        with self._lock:
            endpoints = tuple(
                channel.endpoint for channel in self._channels.values()
            )
            self._channels.clear()
        for endpoint in endpoints:
            endpoint.close()

    def install(
        self,
        binding: ClaudeStructuredBinding,
        oauth: bytearray,
    ) -> None:
        """Install one exact binding on every committed Claude channel."""
        with self._distribution_lock:
            with self._lock:
                channels = tuple(self._channels.items())
            if (
                self.requires_participant(
                    ProviderId.CLAUDE,
                    binding.account_id,
                )
                and not channels
            ):
                raise ClaudeProtectedChannelError(
                    "The protected Claude participant is unavailable."
                )
            for participant_id, channel in channels:
                if channel.binding == binding:
                    continue
                self._install_one(participant_id, channel, binding, oauth)

    def _install_one(
        self,
        participant_id: ParticipantId,
        channel: _ClaudeParticipantChannel,
        binding: ClaudeStructuredBinding,
        oauth: bytearray,
    ) -> None:
        nonce = new_request_id()
        payload = encode_protected_projection(
            binding,
            oauth,
            nonce,
            participant_id=participant_id,
            connection_generation=channel.connection_generation,
        )
        frame: bytearray | None = None
        try:
            frame = encode_bounded_frame(
                payload,
                MAX_CLAUDE_PROTECTED_FRAME_BYTES,
            )
            channel.endpoint.settimeout(CLAUDE_PROTECTED_RESPONSE_SECONDS)
            channel.endpoint.sendall(frame)
            receipt = receive_protected_socket_frame(channel.endpoint)
            install_receipt = require_protected_install_receipt(
                receipt,
                binding,
                nonce,
                participant_id,
                channel.connection_generation,
            )
            with self._lock:
                current = self._channels.get(participant_id)
                if current is not channel:
                    raise ClaudeProtectedChannelError(
                        "The protected participant reconnected during install."
                    )
                current.binding = install_receipt.binding
        except ClaudeProtectedChannelError, OSError, ValueError:
            self._discard_failed(participant_id, channel)
            raise ClaudeProtectedChannelError(
                "The protected Claude install was not acknowledged."
            ) from None
        finally:
            clear_mutable_buffer(payload)
            if frame is not None:
                clear_mutable_buffer(frame)

    def _discard_failed(
        self,
        participant_id: ParticipantId,
        channel: _ClaudeParticipantChannel,
    ) -> None:
        with self._lock:
            if self._channels.get(participant_id) is not channel:
                return
            self._channels.pop(participant_id)
        channel.endpoint.close()
        if self._participant_failed is not None:
            self._participant_failed(
                participant_id,
                channel.connection_generation,
            )

    def _commit(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
        endpoint: socket.socket,
    ) -> _ClaudeParticipantChannel | None:
        with self._lock:
            current = self._channels.get(participant_id)
            if current is not None and (
                connection_generation <= current.connection_generation
            ):
                raise ClaudeProtectedChannelError(
                    "The protected participant binding was replayed."
                )
            self._channels[participant_id] = _ClaudeParticipantChannel(
                endpoint,
                connection_generation,
                peer,
            )
            return current

    def _rollback(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
        replaced: _ClaudeParticipantChannel | None,
    ) -> socket.socket | None:
        with self._lock:
            current = self._channels.get(participant_id)
            if current is None or (
                current.connection_generation != connection_generation
                or current.peer != peer
            ):
                return None
            if replaced is None:
                self._channels.pop(participant_id)
            else:
                self._channels[participant_id] = replaced
            return current.endpoint

    @staticmethod
    def _require_endpoint(endpoint: socket.socket) -> None:
        if (
            endpoint.family is not socket.AF_UNIX
            or endpoint.type & socket.SOCK_STREAM != socket.SOCK_STREAM
            or endpoint.fileno() < 0
        ):
            raise ClaudeProtectedChannelError(
                "The protected participant endpoint is invalid."
            )

    def __repr__(self) -> str:
        """Return a secret-free registry representation."""
        return "<ClaudeParticipantChannelRegistry protected>"


class ClaudeProtectedHostChannel:
    """Consume and acknowledge projections on one participant endpoint."""

    def __init__(
        self,
        endpoint: socket.socket,
        participant_id: ParticipantId,
        connection_generation: int,
    ) -> None:
        ClaudeParticipantChannelRegistry._require_endpoint(endpoint)
        os.set_inheritable(endpoint.fileno(), False)
        self._endpoint = endpoint
        self._participant_id = participant_id
        self._connection_generation = connection_generation
        self._pending: tuple[ClaudeStructuredBinding, RequestId] | None = None
        self._closed = False

    def receive(self) -> ClaudeProtectedOAuthFrame:
        """Receive one exact mutable projection without retaining a copy."""
        if self._closed or self._pending is not None:
            raise ClaudeProtectedChannelError(
                "The protected host channel is not ready."
            )
        payload = receive_protected_socket_frame(self._endpoint)
        oauth: bytearray | None = None
        try:
            metadata, oauth = decode_protected_projection(payload)
            if metadata.participant_id != self._participant_id or (
                metadata.connection_generation != self._connection_generation
            ):
                raise ClaudeProtectedChannelError(
                    "The protected participant projection does not match."
                )
            self._pending = metadata.binding, metadata.nonce
            frame = ClaudeProtectedOAuthFrame(metadata.binding, oauth)
            oauth = None
            return frame
        finally:
            clear_mutable_buffer(payload)
            if oauth is not None:
                clear_mutable_buffer(oauth)

    def acknowledge(
        self,
        receipt: ClaudeStructuredInstallReceipt,
    ) -> None:
        """Acknowledge one successfully installed exact binding."""
        pending = self._pending
        if self._closed or pending is None or pending[0] != receipt.binding:
            raise ClaudeProtectedChannelError(
                "The protected host acknowledgement does not match."
            )
        payload = encode_protected_install_receipt(
            receipt,
            pending[1],
            self._participant_id,
            self._connection_generation,
        )
        frame = encode_bounded_frame(payload, MAX_CLAUDE_PROTECTED_FRAME_BYTES)
        try:
            self._endpoint.sendall(frame)
            self._pending = None
        except OSError:
            raise ClaudeProtectedChannelClosedError(
                "The protected host acknowledgement failed."
            ) from None
        finally:
            clear_mutable_buffer(frame)

    def release_ambiguous_projection(self) -> None:
        """Release only receipt correlation after an ambiguous install."""
        if self._closed or self._pending is None:
            raise ClaudeProtectedChannelError(
                "The protected host projection is not pending."
            )
        self._pending = None

    def report_current_binding(
        self,
        binding: ClaudeStructuredBinding | None,
    ) -> None:
        """Answer one nonce-correlated supervisor binding query."""
        if self._closed or self._pending is not None:
            raise ClaudeProtectedChannelError(
                "The protected host channel is not ready."
            )
        nonce = require_protected_binding_query(
            receive_protected_socket_frame(self._endpoint),
            self._participant_id,
            self._connection_generation,
        )
        payload = encode_protected_binding_report(
            binding, nonce, self._participant_id, self._connection_generation
        )
        frame = encode_bounded_frame(payload, MAX_CLAUDE_PROTECTED_FRAME_BYTES)
        try:
            self._endpoint.sendall(frame)
        except OSError:
            raise ClaudeProtectedChannelClosedError(
                "The protected host binding report failed."
            ) from None
        finally:
            clear_mutable_buffer(frame)

    def close(self) -> None:
        """Close only this host-owned protected endpoint."""
        if self._closed:
            return
        self._closed = True
        self._pending = None
        with suppress(OSError):
            self._endpoint.shutdown(socket.SHUT_RDWR)
        self._endpoint.close()

    def __repr__(self) -> str:
        """Return no participant or credential material."""
        return "<ClaudeProtectedHostChannel protected>"


class ClaudeProtectedSupervisorExchange(Protocol):
    """Resident half of one existing bounded worker exchange."""

    def receive_response(self) -> bytearray:
        """Receive the sole worker response."""

    def acknowledge(self, payload: bytes | bytearray) -> None:
        """Acknowledge receipt without returning a credential."""


class ClaudeProtectedExchangeRegistry(Protocol):
    """Create and cancel exact child exchanges owned by the scheduler."""

    def create(
        self,
        operation_id: OperationId,
        instruction: bytes,
        response_deadline: float,
        completion_deadline: float,
    ) -> ClaudeProtectedSupervisorExchange:
        """Create one exchange before the child is enqueued."""

    def cancel(self, operation_id: OperationId) -> None:
        """Cancel the exact child exchange."""


class ClaudeProtectedWorkerSubmission(Protocol):
    """Worker response awaiting one safe supervisor receipt."""

    def receive_acknowledgement(self) -> bytearray:
        """Return the sole safe acknowledgement."""


class ClaudeProtectedWorkerExchange(Protocol):
    """Worker half of one existing bounded exchange."""

    def receive_instruction(self) -> bytearray:
        """Receive one safe exact-child instruction."""

    def submit(
        self,
        payload: bytearray,
        response_deadline: float,
        completion_deadline: float,
    ) -> ClaudeProtectedWorkerSubmission:
        """Submit one mutable projection and clear the payload."""


class ClaudeProtectedCompletion(Protocol):
    """Safe selection metadata required after scheduler completion."""

    @property
    def kind(self) -> OperationKind:
        """Return the exact selection child kind."""

    @property
    def observed_account_id(self) -> SidekickAccountId | None:
        """Return the worker-observed exact account."""

    @property
    def observed_generation(self) -> AuthorityGeneration | None:
        """Return the worker-observed exact generation."""

    @property
    def pending_epoch(self) -> SelectionEpoch:
        """Return the pending selection epoch."""


@dataclass(slots=True)
class _PendingProjection:
    parent_operation_id: OperationId
    kind: OperationKind
    nonce: RequestId
    exchange: ClaudeProtectedSupervisorExchange
    frame: ClaudeProtectedOAuthFrame | None = None


class ClaudeProtectedCommitRelay:
    """Relay one worker lease only after durable scheduler completion."""

    def __init__(
        self,
        exchanges: ClaudeProtectedExchangeRegistry,
        channels: ClaudeParticipantChannelRegistry,
    ) -> None:
        self._exchanges = exchanges
        self._channels = channels
        self._lock = Lock()
        self._pending: dict[OperationId, _PendingProjection] = {}

    def prepare(
        self,
        child_operation_id: OperationId,
        parent_operation_id: OperationId,
        provider_id: ProviderId,
        kind: OperationKind,
    ) -> bool:
        """Create one exact Claude exchange before durable enqueue."""
        if provider_id is not ProviderId.CLAUDE or (
            kind not in _CLAUDE_PROJECTION_KINDS
        ):
            return False
        nonce = new_request_id()
        response_deadline = monotonic() + CLAUDE_PROTECTED_RESPONSE_SECONDS
        completion_deadline = monotonic() + CLAUDE_PROTECTED_COMPLETION_SECONDS
        instruction = encode_protected_exchange_instruction(
            ClaudeProtectedExchangeInstruction(
                child_operation_id=child_operation_id,
                parent_operation_id=parent_operation_id,
                provider_id=provider_id,
                kind=kind,
                nonce=nonce,
                response_deadline=response_deadline,
                completion_deadline=completion_deadline,
            )
        )
        exchange = self._exchanges.create(
            child_operation_id,
            instruction,
            response_deadline,
            completion_deadline,
        )
        with self._lock:
            if child_operation_id in self._pending:
                self._exchanges.cancel(child_operation_id)
                raise ClaudeProtectedChannelError(
                    "The protected child exchange was replayed."
                )
            self._pending[child_operation_id] = _PendingProjection(
                parent_operation_id,
                kind,
                nonce,
                exchange,
            )
        return True

    def receive(self, child_operation_id: OperationId) -> None:
        """Receive and acknowledge one projection before worker exit."""
        pending = self._require_pending(child_operation_id)
        payload = pending.exchange.receive_response()
        oauth: bytearray | None = None
        try:
            metadata, oauth = decode_protected_projection(payload)
            if (
                metadata.child_operation_id != child_operation_id
                or metadata.binding.operation_id != pending.parent_operation_id
                or metadata.nonce != pending.nonce
            ):
                raise ClaudeProtectedChannelError(
                    "The protected projection binding does not match."
                )
            pending.frame = ClaudeProtectedOAuthFrame(
                metadata.binding,
                oauth,
            )
            oauth = None
            pending.exchange.acknowledge(
                encode_protected_ack(child_operation_id, pending.nonce)
            )
        finally:
            clear_mutable_buffer(payload)
            if oauth is not None:
                clear_mutable_buffer(oauth)

    def complete(
        self,
        child_operation_id: OperationId,
        metadata: ClaudeProtectedCompletion,
    ) -> None:
        """Fan out only after the scheduler publishes exact success."""
        pending = self._pop_pending(child_operation_id)
        frame = pending.frame
        if frame is None:
            raise ClaudeProtectedChannelError(
                "The protected worker projection is missing."
            )
        binding = frame.protected_binding
        if (
            binding.operation_id != pending.parent_operation_id
            or metadata.kind is not pending.kind
            or binding.account_id != metadata.observed_account_id
            or binding.generation != metadata.observed_generation
            or binding.epoch != metadata.pending_epoch
        ):
            frame.close_protected_frame()
            raise ClaudeProtectedChannelError(
                "The protected worker projection does not match completion."
            )
        oauth = frame.take_protected_oauth()
        try:
            self._channels.install(binding, oauth)
        finally:
            clear_secret_buffer(oauth)
            frame.close_protected_frame()

    def abort(self, child_operation_id: OperationId) -> None:
        """Cancel and clear only the exact failed child projection."""
        with self._lock:
            pending = self._pending.pop(child_operation_id, None)
        self._exchanges.cancel(child_operation_id)
        if pending is not None and pending.frame is not None:
            pending.frame.close_protected_frame()

    def _require_pending(
        self,
        child_operation_id: OperationId,
    ) -> _PendingProjection:
        with self._lock:
            pending = self._pending.get(child_operation_id)
        if pending is None:
            raise ClaudeProtectedChannelError(
                "The protected child exchange is unavailable."
            )
        return pending

    def _pop_pending(
        self,
        child_operation_id: OperationId,
    ) -> _PendingProjection:
        with self._lock:
            pending = self._pending.pop(child_operation_id, None)
        if pending is None:
            raise ClaudeProtectedChannelError(
                "The protected child exchange is unavailable."
            )
        return pending


class ClaudeProtectedProjectionWriter:
    """Write one operation-bound mutable lease from an isolated worker."""

    def __init__(
        self,
        exchange: ClaudeProtectedWorkerExchange,
        child_operation_id: OperationId,
        parent_operation_id: OperationId,
        kind: OperationKind,
    ) -> None:
        instruction = exchange.receive_instruction()
        try:
            metadata = decode_protected_exchange_instruction(instruction)
        finally:
            clear_mutable_buffer(instruction)
        if (
            metadata.child_operation_id != child_operation_id
            or metadata.parent_operation_id != parent_operation_id
            or metadata.provider_id is not ProviderId.CLAUDE
            or metadata.kind is not kind
        ):
            raise ClaudeProtectedChannelError(
                "The protected worker instruction does not match."
            )
        self._exchange = exchange
        self._child_operation_id = child_operation_id
        self._parent_operation_id = parent_operation_id
        self._nonce = metadata.nonce
        self._response_deadline = metadata.response_deadline
        self._completion_deadline = metadata.completion_deadline

    def submit(
        self,
        binding: ClaudeStructuredBinding,
        oauth: bytearray,
    ) -> None:
        """Submit and clear one exact projection without retry."""
        payload = encode_protected_projection(
            binding,
            oauth,
            self._nonce,
            child_operation_id=self._child_operation_id,
        )
        acknowledgement = self._exchange.submit(
            payload,
            self._response_deadline,
            self._completion_deadline,
        ).receive_acknowledgement()
        require_protected_ack(
            acknowledgement,
            self._child_operation_id,
            self._nonce,
        )

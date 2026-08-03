"""Correlated local quiescence proof for Codex participants."""

import os
import socket
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Protocol, Self

from sidekick_usages.core.accounts.identifiers import new_request_id
from sidekick_usages.core.accounts.types import OperationId, RequestId
from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    FinalizedSelection,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import ParticipantId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.platform.models import ProcessIdentity
from sidekick_usages.providers.codex.session.models import CodexRelayAuthority
from sidekick_usages.serialization.json import (
    JsonObject,
    decode_json_object,
    encode_compact_json,
)

_PROTOCOL_VERSION = 2
_PROOF_TIMEOUT_SECONDS = 8.0
_MAX_PARTICIPANTS = 16
_CHALLENGE_KEYS = frozenset(
    {
        "challenge",
        "epoch",
        "operation_id",
        "phase",
        "protocol_version",
        "refresh_required",
    }
)
_RECEIPT_KEYS = _CHALLENGE_KEYS | {
    "loaded_thread_count",
    "quiescent",
    "revision",
}
type CodexProofTransportFactory = Callable[
    [socket.socket], CodexProofTransport
]


class CodexParticipantProofError(RuntimeError):
    """Reject malformed, stale, or unreachable participant proof."""


class _ProofPhase(StrEnum):
    """Closed participant proof phases around external-auth mutation."""

    PRECOMMIT = "precommit"
    POSTCOMMIT = "postcommit"
    ABORT = "abort"


@dataclass(frozen=True, slots=True)
class _Challenge:
    operation_id: OperationId
    epoch: SelectionEpoch
    phase: _ProofPhase
    challenge: RequestId
    refresh_required: bool


@dataclass(frozen=True, slots=True)
class _Receipt:
    challenge: _Challenge
    revision: int
    loaded_thread_count: int
    quiescent: bool


class CodexQuiescenceRelay(Protocol):
    """Return secret-free quiescence from one participant-local relay."""

    def arm_quiescence(
        self,
        refresh_required: bool,
    ) -> tuple[int, int, bool]:
        """Arm and prove the precommit participant barrier."""

    def confirm_quiescence(
        self,
        refresh_required: bool,
    ) -> tuple[int, int, bool]:
        """Reprove under the retained participant barrier."""

    def release_quiescence(self) -> tuple[int, int, bool]:
        """Release the barrier without another provider read."""

    def discard_quiescence(self) -> None:
        """Release a failed proof barrier without masking its failure."""


class CodexProofTransport(Protocol):
    """Exchange already validated bounded local proof payloads."""

    def receive_payload(self) -> bytes:
        """Receive one complete payload or raise a transport failure."""

    def send_payload(self, payload: bytes) -> None:
        """Send one complete payload or raise a transport failure."""

    def close(self) -> None:
        """Close the connected local stream."""


@dataclass(slots=True)
class _Channel:
    endpoint: socket.socket
    transport: CodexProofTransport
    connection_generation: int
    peer: ProcessIdentity
    binding: tuple[OperationId, CodexRelayAuthority] | None = None


@dataclass(slots=True)
class _PendingProof:
    operation_id: OperationId
    epoch: SelectionEpoch
    channels: tuple[tuple[ParticipantId, _Channel], ...]
    refresh_required: bool
    receipts: dict[ParticipantId, tuple[int, int]]
    awaiting_terminal: set[ParticipantId]


class _AttachmentTransaction:
    """Transfer one staged Codex proof endpoint exactly once."""

    def __init__(
        self,
        proofs: CodexParticipantProofSet,
        participant_id: ParticipantId,
        generation: int,
        peer: ProcessIdentity,
        endpoint: socket.socket,
    ) -> None:
        self._proofs = proofs
        self._participant_id = participant_id
        self._generation = generation
        self._peer = peer
        self._endpoint: socket.socket | None = endpoint
        self._replaced: _Channel | None = None
        self._committed = False

    def commit(self) -> None:
        """Commit the endpoint before participant membership mutates."""
        endpoint = self._take_endpoint()
        try:
            self._replaced = self._proofs._commit(
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
        """Release only the endpoint replaced by the committed generation."""
        replaced = self._replaced
        self._replaced = None
        self._committed = False
        if replaced is not None:
            replaced.transport.close()

    def rollback(self) -> None:
        """Restore a replaced endpoint or close the uncommitted endpoint."""
        endpoint = self._endpoint
        self._endpoint = None
        replaced = self._replaced
        self._replaced = None
        if self._committed:
            endpoint = self._proofs._rollback(
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
        if endpoint is None:
            raise CodexParticipantProofError(
                "The Codex attachment transaction is closed."
            )
        self._endpoint = None
        return endpoint


class CodexParticipantProofSet:
    """Own exact broker-side endpoints for sealed Codex participants."""

    def __init__(
        self,
        transport_factory: CodexProofTransportFactory,
    ) -> None:
        self._lock = Lock()
        self._distribution = Lock()
        self._transport_factory = transport_factory
        self._channels: dict[ParticipantId, _Channel] = {}
        self._pending: _PendingProof | None = None

    @staticmethod
    def requires_endpoint(provider_id: ProviderId) -> bool:
        """Require one proof endpoint for each Codex participant."""
        return provider_id is ProviderId.CODEX

    @staticmethod
    def requires_finalized_binding(provider_id: ProviderId) -> bool:
        """Return false because Codex baseline authority is process-wide."""
        del provider_id
        return False

    def stage(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
        endpoint: socket.socket,
    ) -> _AttachmentTransaction:
        """Validate and stage one peer-verified socketpair endpoint."""
        try:
            _require_endpoint(endpoint)
            with self._lock:
                current = self._channels.get(participant_id)
                if current is not None and (
                    connection_generation <= current.connection_generation
                ):
                    raise CodexParticipantProofError(
                        "The Codex participant attachment was replayed."
                    )
                if (
                    current is None
                    and len(self._channels) >= _MAX_PARTICIPANTS
                ):
                    raise CodexParticipantProofError(
                        "The Codex participant capacity was reached."
                    )
            os.set_inheritable(endpoint.fileno(), False)
        except BaseException:
            endpoint.close()
            raise
        return _AttachmentTransaction(
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
        """Close only the endpoint matching one proved dead process."""
        with self._lock:
            channel = self._channels.get(participant_id)
            if channel is None:
                return
            if (
                channel.connection_generation != connection_generation
                or channel.peer != peer
            ):
                raise CodexParticipantProofError(
                    "The Codex participant attachment does not match."
                )
            self._channels.pop(participant_id)
        channel.transport.close()

    def prepare(
        self,
        operation_id: OperationId,
        epoch: SelectionEpoch,
    ) -> None:
        """Prove the exact participant set immediately before mutation."""
        self._distribution.acquire()
        try:
            with self._lock:
                if self._pending is not None:
                    raise CodexParticipantProofError(
                        "Another Codex participant proof is active."
                    )
                channels = tuple(self._channels.items())
                pending = _PendingProof(
                    operation_id,
                    epoch,
                    channels,
                    True,
                    {},
                    set(),
                )
                self._pending = pending
            self._exchange(
                channels,
                operation_id,
                epoch,
                _ProofPhase.PRECOMMIT,
                pending,
            )
        except BaseException:
            self._abort(operation_id, epoch)
            self._distribution.release()
            raise

    def complete(
        self,
        operation_id: OperationId,
        target: CodexRelayAuthority,
    ) -> None:
        """Reprove the unchanged set and bind it to the committed target."""
        completed = False
        try:
            with self._lock:
                pending = self._pending
            if pending is None or (
                pending.operation_id != operation_id
                or pending.epoch != target.epoch
            ):
                raise CodexParticipantProofError(
                    "The Codex participant proof is not pending."
                )
            channels = pending.channels
            receipts = self._exchange(
                channels,
                operation_id,
                target.epoch,
                _ProofPhase.POSTCOMMIT,
                pending,
            )
            if receipts != pending.receipts:
                raise CodexParticipantProofError(
                    "Codex participant state changed during selection."
                )
            with self._lock:
                if tuple(self._channels.items()) != channels:
                    raise CodexParticipantProofError(
                        "Codex participant membership changed during proof."
                    )
                for channel in self._channels.values():
                    channel.binding = operation_id, target
                self._pending = None
            completed = True
        finally:
            if completed:
                self._distribution.release()

    def abort(
        self,
        operation_id: OperationId,
        epoch: SelectionEpoch,
    ) -> None:
        """Release a prepared participant set without changing bindings."""
        if self._abort(operation_id, epoch):
            self._distribution.release()

    def bind_after_readback(
        self,
        operation_id: OperationId,
        target: CodexRelayAuthority,
    ) -> None:
        """Bind only late channels after exact target authority readback."""
        self._distribution.acquire()
        pending: _PendingProof | None = None
        try:
            with self._lock:
                if self._pending is not None:
                    raise CodexParticipantProofError(
                        "Another Codex participant proof is active."
                    )
                binding = operation_id, target
                channels = tuple(
                    (participant_id, channel)
                    for participant_id, channel in self._channels.items()
                    if channel.binding != binding
                )
                if not channels:
                    return
                pending = _PendingProof(
                    operation_id,
                    target.epoch,
                    channels,
                    False,
                    {},
                    set(),
                )
                self._pending = pending
            self._exchange(
                channels,
                operation_id,
                target.epoch,
                _ProofPhase.PRECOMMIT,
                pending,
            )
            receipts = self._exchange(
                channels,
                operation_id,
                target.epoch,
                _ProofPhase.POSTCOMMIT,
                pending,
            )
            if receipts != pending.receipts:
                raise CodexParticipantProofError(
                    "Codex participant state changed during selection."
                )
            with self._lock:
                if any(
                    self._channels.get(participant_id) is not channel
                    for participant_id, channel in channels
                ):
                    raise CodexParticipantProofError(
                        "Codex participant membership changed during proof."
                    )
                for _participant_id, channel in channels:
                    channel.binding = binding
                self._pending = None
        except BaseException:
            if pending is not None:
                self._abort(operation_id, target.epoch)
            raise
        finally:
            self._distribution.release()

    def matches_target(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
        operation_id: OperationId,
        proof: AuthorityReadyProof,
    ) -> bool:
        """Return whether one endpoint completed both proof phases."""
        target = CodexRelayAuthority(
            account_id=proof.account_id,
            generation=proof.generation,
            epoch=proof.epoch,
        )
        with self._lock:
            channel = self._channels.get(participant_id)
            return channel is not None and (
                channel.connection_generation == connection_generation
                and channel.peer == peer
                and channel.binding == (operation_id, target)
            )

    def matches_finalized(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        peer: ProcessIdentity,
        operation_id: OperationId,
        finalized: FinalizedSelection,
    ) -> bool:
        """Match a finalized target only when it was already proven."""
        target = CodexRelayAuthority(
            account_id=finalized.account_id,
            generation=finalized.generation,
            epoch=finalized.epoch,
        )
        with self._lock:
            channel = self._channels.get(participant_id)
            return channel is not None and (
                channel.connection_generation == connection_generation
                and channel.peer == peer
                and channel.binding == (operation_id, target)
            )

    def close(self) -> None:
        """Close all broker-owned proof endpoints."""
        with self._lock:
            channels = tuple(self._channels.values())
            self._channels.clear()
            self._pending = None
        for channel in channels:
            channel.transport.close()

    def _exchange(
        self,
        channels: tuple[tuple[ParticipantId, _Channel], ...],
        operation_id: OperationId,
        epoch: SelectionEpoch,
        phase: _ProofPhase,
        pending: _PendingProof,
    ) -> dict[ParticipantId, tuple[int, int]]:
        receipts: dict[ParticipantId, tuple[int, int]] = {}
        for participant_id, channel in channels:
            challenge = _Challenge(
                operation_id,
                epoch,
                phase,
                new_request_id(),
                pending.refresh_required,
            )
            channel.endpoint.settimeout(_PROOF_TIMEOUT_SECONDS)
            try:
                channel.transport.send_payload(_encode_challenge(challenge))
                if phase is _ProofPhase.PRECOMMIT:
                    pending.awaiting_terminal.add(participant_id)
                receipt = _decode_receipt(channel.transport.receive_payload())
            except OSError, ValueError:
                raise CodexParticipantProofError(
                    "The Codex participant proof channel failed."
                ) from None
            if phase is _ProofPhase.POSTCOMMIT:
                pending.awaiting_terminal.discard(participant_id)
            correlation = (
                receipt.revision,
                receipt.loaded_thread_count,
            )
            receipts[participant_id] = correlation
            if phase is _ProofPhase.PRECOMMIT:
                pending.receipts[participant_id] = correlation
            if receipt.challenge != challenge or not receipt.quiescent:
                raise CodexParticipantProofError(
                    "Codex participant quiescence was not proven."
                )
        return receipts

    def _abort(
        self,
        operation_id: OperationId,
        epoch: SelectionEpoch,
    ) -> bool:
        with self._lock:
            pending = self._pending
            self._pending = None
        if pending is None or (
            pending.operation_id != operation_id or pending.epoch != epoch
        ):
            return False
        for participant_id, channel in pending.channels:
            if participant_id not in pending.awaiting_terminal:
                continue
            challenge = _Challenge(
                operation_id,
                epoch,
                _ProofPhase.ABORT,
                new_request_id(),
                pending.refresh_required,
            )
            try:
                channel.endpoint.settimeout(_PROOF_TIMEOUT_SECONDS)
                channel.transport.send_payload(_encode_challenge(challenge))
                _decode_receipt(channel.transport.receive_payload())
            except CodexParticipantProofError, OSError, ValueError:
                continue
        return True

    def _commit(
        self,
        participant_id: ParticipantId,
        generation: int,
        peer: ProcessIdentity,
        endpoint: socket.socket,
    ) -> _Channel | None:
        with self._lock:
            current = self._channels.get(participant_id)
            if current is not None and (
                generation <= current.connection_generation
            ):
                raise CodexParticipantProofError(
                    "The Codex participant attachment was replayed."
                )
            self._channels[participant_id] = _Channel(
                endpoint,
                self._transport_factory(endpoint),
                generation,
                peer,
            )
            return current

    def _rollback(
        self,
        participant_id: ParticipantId,
        generation: int,
        peer: ProcessIdentity,
        replaced: _Channel | None,
    ) -> socket.socket | None:
        with self._lock:
            current = self._channels.get(participant_id)
            if current is None or (
                current.connection_generation != generation
                or current.peer != peer
            ):
                return None
            if replaced is None:
                self._channels.pop(participant_id)
            else:
                self._channels[participant_id] = replaced
            return current.endpoint


class CodexParticipantProofChannel:
    """Serve one selection proof on a participant-owned socketpair half."""

    @classmethod
    def create(
        cls,
        transport_factory: CodexProofTransportFactory,
    ) -> tuple[Self, socket.socket]:
        """Create both private halves for one participant registration."""
        participant, supervisor = socket.socketpair()
        try:
            return cls(participant, transport_factory), supervisor
        except BaseException:
            participant.close()
            supervisor.close()
            raise

    def __init__(
        self,
        endpoint: socket.socket,
        transport_factory: CodexProofTransportFactory,
    ) -> None:
        _require_endpoint(endpoint)
        os.set_inheritable(endpoint.fileno(), False)
        self._transport = transport_factory(endpoint)

    def serve_selection(
        self,
        relay: CodexQuiescenceRelay,
        epoch: SelectionEpoch,
    ) -> None:
        """Serve precommit and terminal proof phases for one selection."""
        armed = False
        try:
            first = _decode_challenge(self._transport.receive_payload())
            self._require_challenge(
                first,
                first.operation_id,
                epoch,
                {_ProofPhase.PRECOMMIT},
            )
            self._respond(
                first,
                relay.arm_quiescence(first.refresh_required),
            )
            armed = True
            terminal = _decode_challenge(self._transport.receive_payload())
            self._require_challenge(
                terminal,
                first.operation_id,
                epoch,
                {_ProofPhase.POSTCOMMIT, _ProofPhase.ABORT},
            )
            if terminal.refresh_required is not first.refresh_required:
                raise CodexParticipantProofError(
                    "The Codex participant challenge does not match."
                )
            if terminal.phase is _ProofPhase.ABORT:
                released = relay.release_quiescence()
                armed = False
                self._respond(terminal, released)
            else:
                confirmed = relay.confirm_quiescence(
                    terminal.refresh_required,
                )
                self._respond(
                    terminal,
                    confirmed,
                )
                if not confirmed[2]:
                    relay.discard_quiescence()
                armed = False
        finally:
            if armed:
                relay.discard_quiescence()

    def close(self) -> None:
        """Close only this participant-owned proof channel."""
        self._transport.close()

    def _respond(
        self,
        challenge: _Challenge,
        proof: tuple[int, int, bool],
    ) -> None:
        revision, count, quiescent = proof
        self._transport.send_payload(
            _encode_receipt(_Receipt(challenge, revision, count, quiescent))
        )

    @staticmethod
    def _require_challenge(
        challenge: _Challenge,
        operation_id: OperationId,
        epoch: SelectionEpoch,
        phases: set[_ProofPhase],
    ) -> None:
        if (
            challenge.operation_id != operation_id
            or challenge.epoch != epoch
            or challenge.phase not in phases
        ):
            raise CodexParticipantProofError(
                "The Codex participant challenge does not match."
            )


def _require_endpoint(endpoint: socket.socket) -> None:
    if (
        endpoint.family is not socket.AF_UNIX
        or endpoint.type & socket.SOCK_STREAM != socket.SOCK_STREAM
        or endpoint.fileno() < 0
    ):
        raise CodexParticipantProofError(
            "The Codex participant proof endpoint is invalid."
        )


def _encode_challenge(challenge: _Challenge) -> bytes:
    return encode_compact_json(
        {
            "challenge": str(challenge.challenge),
            "epoch": challenge.epoch.value,
            "operation_id": str(challenge.operation_id),
            "phase": challenge.phase.value,
            "protocol_version": _PROTOCOL_VERSION,
            "refresh_required": challenge.refresh_required,
        }
    )


def _decode_challenge(payload: bytes) -> _Challenge:
    root = _decode(payload, _CHALLENGE_KEYS)
    try:
        return _Challenge(
            OperationId(_text(root, "operation_id")),
            SelectionEpoch(_integer(root, "epoch")),
            _ProofPhase(_text(root, "phase")),
            RequestId(_text(root, "challenge")),
            _boolean(root, "refresh_required"),
        )
    except ValueError:
        raise CodexParticipantProofError(
            "The Codex participant challenge is malformed."
        ) from None


def _encode_receipt(receipt: _Receipt) -> bytes:
    challenge = receipt.challenge
    return encode_compact_json(
        {
            "challenge": str(challenge.challenge),
            "epoch": challenge.epoch.value,
            "loaded_thread_count": receipt.loaded_thread_count,
            "operation_id": str(challenge.operation_id),
            "phase": challenge.phase.value,
            "protocol_version": _PROTOCOL_VERSION,
            "quiescent": receipt.quiescent,
            "refresh_required": challenge.refresh_required,
            "revision": receipt.revision,
        }
    )


def _decode_receipt(payload: bytes) -> _Receipt:
    root = _decode(payload, _RECEIPT_KEYS)
    challenge = _decode_challenge(
        encode_compact_json(
            {
                name: value
                for name, value in root.items()
                if name in _CHALLENGE_KEYS
            }
        )
    )
    revision = _integer(root, "revision")
    count = _integer(root, "loaded_thread_count")
    quiescent = root.get("quiescent")
    if isinstance(quiescent, bool) and revision >= 0 and count >= 0:
        return _Receipt(challenge, revision, count, quiescent)
    raise CodexParticipantProofError(
        "The Codex participant receipt is malformed."
    )


def _decode(payload: bytes, keys: frozenset[str] | set[str]) -> JsonObject:
    try:
        root = decode_json_object(payload)
    except InvalidPayloadError:
        raise CodexParticipantProofError(
            "The Codex participant frame is malformed."
        ) from None
    if set(root) != keys or root.get("protocol_version") != _PROTOCOL_VERSION:
        raise CodexParticipantProofError(
            "The Codex participant frame is malformed."
        )
    return root


def _text(root: JsonObject, name: str) -> str:
    value = root.get(name)
    if not isinstance(value, str):
        raise CodexParticipantProofError(
            "The Codex participant frame is malformed."
        )
    return value


def _integer(root: JsonObject, name: str) -> int:
    value = root.get(name)
    if type(value) is not int:
        raise CodexParticipantProofError(
            "The Codex participant frame is malformed."
        )
    return value


def _boolean(root: JsonObject, name: str) -> bool:
    value = root.get(name)
    if not isinstance(value, bool):
        raise CodexParticipantProofError(
            "The Codex participant frame is malformed."
        )
    return value

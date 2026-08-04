"""Idle-only OAuth updates for one structured Claude participant."""

from collections.abc import Callable
from typing import NoReturn
from uuid import uuid4

from sidekick_usages.core.accounts.types import RequestId
from sidekick_usages.core.accounts.validation import (
    MAX_OPAQUE_BYTES,
    require_bounded_text,
)
from sidekick_usages.core.selection.types import TurnId
from sidekick_usages.providers.claude.auth.generation import (
    claude_access_token_buffer_generation,
)
from sidekick_usages.providers.claude.structured.codec import (
    clear_secret_buffer,
    decode_oauth_update_success,
    encode_oauth_update,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredActivityKind,
    ClaudeStructuredActivityState,
    ClaudeStructuredAdoptionReceipt,
    ClaudeStructuredBinding,
    ClaudeStructuredConversationId,
    ClaudeStructuredEngine,
    ClaudeStructuredError,
    ClaudeStructuredFailure,
    ClaudeStructuredInstallReceipt,
    ClaudeStructuredProtectedFrame,
    ClaudeStructuredStreamEvent,
    ClaudeStructuredTurnTransmitter,
)

_CONTROL_TIMEOUT_SECONDS = 5.0


def _new_request_id() -> RequestId:
    return RequestId(str(uuid4()))


class ClaudeStructuredSession:
    """Track exact activity and authority for one unchanged engine."""

    def __init__(
        self,
        engine: ClaudeStructuredEngine,
        binding: ClaudeStructuredBinding,
        *,
        request_id_factory: Callable[[], RequestId] = _new_request_id,
    ) -> None:
        self._engine = engine
        self._binding: ClaudeStructuredBinding = binding
        self._conversation_id: ClaudeStructuredConversationId | None = None
        self._request_id_factory = request_id_factory
        self._pending: ClaudeStructuredBinding | None = None
        self._activities: set[tuple[ClaudeStructuredActivityKind, str]] = set()
        self._turns: dict[TurnId, ClaudeStructuredBinding] = {}
        self._consumed_request_ids: set[RequestId] = set()
        self._issued_request_ids: set[RequestId] = set()

    @classmethod
    def bootstrap(
        cls,
        engine: ClaudeStructuredEngine,
        frame: ClaudeStructuredProtectedFrame,
        *,
        request_id_factory: Callable[[], RequestId] = _new_request_id,
    ) -> tuple[ClaudeStructuredSession, ClaudeStructuredInstallReceipt]:
        """Install initial authority before constructing a bound session."""
        binding = frame.protected_binding
        consumed_request_ids: set[RequestId] = set()
        issued_request_ids: set[RequestId] = set()
        receipt = cls._install_oauth(
            engine,
            frame,
            binding,
            request_id_factory,
            consumed_request_ids,
            issued_request_ids,
        )
        session = cls(
            engine,
            binding,
            request_id_factory=request_id_factory,
        )
        session._consumed_request_ids = consumed_request_ids
        session._issued_request_ids = issued_request_ids
        return session, receipt

    @property
    def process_id(self) -> int:
        """Return the unchanged official engine PID."""
        return self._engine.process_id

    @property
    def binding(self) -> ClaudeStructuredBinding:
        """Return the last exactly acknowledged authority binding."""
        return self._binding

    @property
    def conversation_id(self) -> ClaudeStructuredConversationId | None:
        """Return the unchanged provider-emitted conversation identity."""
        return self._conversation_id

    def prepare_target(self, binding: ClaudeStructuredBinding) -> None:
        """Bind one exact post-commit target without changing authority."""
        if binding.epoch != self._binding.epoch.next():
            self._authority_mismatch()
        if self._pending is None:
            self._pending = binding
            return
        if self._pending != binding:
            self._authority_mismatch()

    def begin_turn(
        self,
        turn_id: TurnId,
        binding: ClaudeStructuredBinding,
    ) -> None:
        """Track one visible admitted turn under its exact binding."""
        if binding != self._binding or turn_id in self._turns:
            self._activity_invalid()
        self._turns[turn_id] = binding

    def end_turn(self, turn_id: TurnId) -> None:
        """Drain one exact visible turn naturally."""
        if self._turns.pop(turn_id, None) is None:
            self._activity_invalid()

    def observe_event(self, event: ClaudeStructuredStreamEvent) -> None:
        """Derive one idle-gate transition from a strict stream event."""
        if event.conversation_id is not None:
            self.observe_conversation(event.conversation_id)
        self.observe_activity(
            event.activity_kind,
            event.activity_id,
            event.activity_state,
        )

    def observe_activity(
        self,
        kind: ClaudeStructuredActivityKind,
        activity_id: str,
        state: ClaudeStructuredActivityState,
    ) -> None:
        """Track one provider activity without requiring a stream identity."""
        self._require_activity_id(activity_id)
        activity = (kind, activity_id)
        if state is ClaudeStructuredActivityState.STARTED:
            if activity in self._activities:
                self._activity_invalid()
            self._activities.add(activity)
            return
        if activity not in self._activities:
            self._activity_invalid()
        self._activities.remove(activity)

    def observe_conversation(
        self,
        conversation_id: ClaudeStructuredConversationId,
    ) -> None:
        """Bind every provider stream frame to one stable conversation."""
        if self._conversation_id is None:
            self._conversation_id = conversation_id
        elif self._conversation_id != conversation_id:
            raise ClaudeStructuredError(
                ClaudeStructuredFailure.CONVERSATION_MISMATCH
            )

    def update_oauth(
        self,
        frame: ClaudeStructuredProtectedFrame,
    ) -> ClaudeStructuredInstallReceipt:
        """Consume one protected OAuth frame at an idle boundary."""
        pending = self._pending
        if pending is None:
            frame.close_protected_frame()
            self._authority_mismatch()
        if self._turns or self._activities:
            frame.close_protected_frame()
            raise ClaudeStructuredError(
                ClaudeStructuredFailure.ACTIVITY_ACTIVE
            )
        receipt = self._install_oauth(
            self._engine,
            frame,
            pending,
            self._request_id_factory,
            self._consumed_request_ids,
            self._issued_request_ids,
        )
        self._binding = pending
        self._pending = None
        return receipt

    def route_turn(
        self,
        turn_id: TurnId,
        binding: ClaudeStructuredBinding,
        transmit: ClaudeStructuredTurnTransmitter,
    ) -> ClaudeStructuredAdoptionReceipt:
        """Produce adoption before transmitting one real admitted turn."""
        if self._pending is not None or binding != self._binding:
            self._authority_mismatch()
        if turn_id in self._turns:
            self._activity_invalid()
        receipt = ClaudeStructuredAdoptionReceipt(
            turn_id=turn_id,
            binding=binding,
        )
        self._turns[turn_id] = binding
        transmit(receipt)
        return receipt

    @staticmethod
    def _require_activity_id(activity_id: str) -> None:
        try:
            require_bounded_text(
                activity_id,
                name="Claude structured activity ID",
                maximum=MAX_OPAQUE_BYTES,
            )
        except TypeError, ValueError:
            ClaudeStructuredSession._activity_invalid()

    @staticmethod
    def _install_oauth(
        engine: ClaudeStructuredEngine,
        frame: ClaudeStructuredProtectedFrame,
        binding: ClaudeStructuredBinding,
        request_id_factory: Callable[[], RequestId],
        consumed_request_ids: set[RequestId],
        issued_request_ids: set[RequestId],
    ) -> ClaudeStructuredInstallReceipt:
        oauth_buffer: bytearray | None = None
        try:
            if frame.protected_binding != binding:
                ClaudeStructuredSession._authority_mismatch()
            oauth_buffer = frame.take_protected_oauth()
            if (
                claude_access_token_buffer_generation(oauth_buffer)
                != binding.generation
            ):
                ClaudeStructuredSession._authority_mismatch()
            request_id = request_id_factory()
            if request_id in issued_request_ids:
                raise ClaudeStructuredError(
                    ClaudeStructuredFailure.PROTOCOL_MALFORMED
                )
            issued_request_ids.add(request_id)
            request = encode_oauth_update(request_id, oauth_buffer)
            try:
                response = engine.exchange(
                    request,
                    request_id,
                    _CONTROL_TIMEOUT_SECONDS,
                )
            finally:
                clear_secret_buffer(request)
            decode_oauth_update_success(
                response,
                request_id,
                frozenset(consumed_request_ids),
            )
            consumed_request_ids.add(request_id)
            return ClaudeStructuredInstallReceipt(
                binding=binding,
                request_id=request_id,
            )
        finally:
            if oauth_buffer is not None:
                clear_secret_buffer(oauth_buffer)
            frame.close_protected_frame()

    @staticmethod
    def _activity_invalid() -> NoReturn:
        raise ClaudeStructuredError(ClaudeStructuredFailure.ACTIVITY_INVALID)

    @staticmethod
    def _authority_mismatch() -> NoReturn:
        raise ClaudeStructuredError(ClaudeStructuredFailure.AUTHORITY_MISMATCH)

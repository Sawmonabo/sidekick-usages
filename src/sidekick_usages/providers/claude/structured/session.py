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
    ClaudeStructuredAdoptionReceipt,
    ClaudeStructuredBinding,
    ClaudeStructuredEngine,
    ClaudeStructuredError,
    ClaudeStructuredFailure,
    ClaudeStructuredProtectedFrame,
    ClaudeStructuredReadyReceipt,
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
        self._request_id_factory = request_id_factory
        self._pending: ClaudeStructuredBinding | None = None
        self._activities: set[tuple[ClaudeStructuredActivityKind, str]] = set()
        self._turns: dict[TurnId, ClaudeStructuredBinding] = {}
        self._consumed_request_ids: set[RequestId] = set()
        self._issued_request_ids: set[RequestId] = set()

    @property
    def process_id(self) -> int:
        """Return the unchanged official engine PID."""
        return self._engine.process_id

    @property
    def binding(self) -> ClaudeStructuredBinding:
        """Return the last exactly acknowledged authority binding."""
        return self._binding

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

    def begin_activity(
        self,
        kind: ClaudeStructuredActivityKind,
        activity_id: str,
    ) -> None:
        """Track one provider activity that prevents an OAuth update."""
        self._require_activity_id(activity_id)
        activity = (kind, activity_id)
        if activity in self._activities:
            self._activity_invalid()
        self._activities.add(activity)

    def end_activity(
        self,
        kind: ClaudeStructuredActivityKind,
        activity_id: str,
    ) -> None:
        """Drain one exact provider activity without underflow."""
        activity = (kind, activity_id)
        if activity not in self._activities:
            self._activity_invalid()
        self._activities.remove(activity)

    def update_oauth(
        self,
        frame: ClaudeStructuredProtectedFrame,
    ) -> ClaudeStructuredReadyReceipt:
        """Consume one protected OAuth frame at an idle boundary."""
        oauth_buffer: bytearray | None = None
        try:
            pending = self._pending
            if pending is None:
                self._authority_mismatch()
            if self._turns or self._activities:
                raise ClaudeStructuredError(
                    ClaudeStructuredFailure.ACTIVITY_ACTIVE
                )
            if frame.protected_binding != pending:
                self._authority_mismatch()
            oauth_buffer = frame.take_protected_oauth()
            if (
                claude_access_token_buffer_generation(oauth_buffer)
                != pending.generation
            ):
                self._authority_mismatch()
            request_id = self._request_id_factory()
            if request_id in self._issued_request_ids:
                raise ClaudeStructuredError(
                    ClaudeStructuredFailure.PROTOCOL_MALFORMED
                )
            self._issued_request_ids.add(request_id)
            request = encode_oauth_update(request_id, oauth_buffer)
            try:
                response = self._engine.exchange(
                    request,
                    request_id,
                    _CONTROL_TIMEOUT_SECONDS,
                )
            finally:
                clear_secret_buffer(request)
            decode_oauth_update_success(
                response,
                request_id,
                frozenset(self._consumed_request_ids),
            )
            self._consumed_request_ids.add(request_id)
        finally:
            if oauth_buffer is not None:
                clear_secret_buffer(oauth_buffer)
            frame.close_protected_frame()
        self._binding = pending
        self._pending = None
        return ClaudeStructuredReadyReceipt(
            binding=pending,
            request_id=request_id,
        )

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
    def _activity_invalid() -> NoReturn:
        raise ClaudeStructuredError(ClaudeStructuredFailure.ACTIVITY_INVALID)

    @staticmethod
    def _authority_mismatch() -> NoReturn:
        raise ClaudeStructuredError(ClaudeStructuredFailure.AUTHORITY_MISMATCH)

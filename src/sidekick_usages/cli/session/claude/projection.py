"""Protected authority installation for one retained Claude engine."""

from collections.abc import Callable
from threading import RLock
from uuid import uuid4

from sidekick_usages.core.accounts.types import RequestId
from sidekick_usages.providers.claude.structured.codec import (
    clear_secret_buffer,
    decode_initialize_success,
)
from sidekick_usages.providers.claude.structured.data_plane import (
    ClaudeProtectedHostChannel,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredBinding,
    ClaudeStructuredEngine,
    ClaudeStructuredError,
    ClaudeStructuredFailure,
    ClaudeStructuredInstallReceipt,
    ClaudeStructuredProtectedFrame,
)
from sidekick_usages.providers.claude.structured.session import (
    ClaudeStructuredSession,
)
from sidekick_usages.providers.claude.structured.stream import (
    encode_claude_initialize,
)

CLAUDE_ENGINE_EVENT_TIMEOUT_SECONDS = 60.0


def new_claude_request_id() -> RequestId:
    """Return one unique structured control request identifier."""
    return RequestId(str(uuid4()))


class ClaudeProjectionInstaller:
    """Install exact protected projections without replacing the engine."""

    def __init__(
        self,
        engine: ClaudeStructuredEngine,
        engine_lock: RLock,
        session_lock: RLock,
        request_id_factory: Callable[[], RequestId] | None,
    ) -> None:
        self._engine = engine
        self._engine_lock = engine_lock
        self._session_lock = session_lock
        self._request_id_factory = request_id_factory
        self._ambiguous_bootstrap: ClaudeStructuredBinding | None = None
        self._initialized = False

    def bind_initial(
        self,
        frame: ClaudeStructuredProtectedFrame,
    ) -> tuple[ClaudeStructuredSession, ClaudeStructuredInstallReceipt]:
        """Install one first authority independently of engine readiness."""
        with self._engine_lock, self._session_lock:
            factory = self._request_id_factory
            if factory is None:
                session, receipt = ClaudeStructuredSession.bootstrap(
                    self._engine,
                    frame,
                )
            else:
                session, receipt = ClaudeStructuredSession.bootstrap(
                    self._engine,
                    frame,
                    request_id_factory=factory,
                )
            self._ambiguous_bootstrap = None
            return session, receipt

    def initialize(self) -> None:
        """Prove engine readiness once without replaying an authority."""
        with self._engine_lock:
            if self._initialized:
                return
            factory = self._request_id_factory
            request_id = (
                new_claude_request_id() if factory is None else factory()
            )
            frame = encode_claude_initialize(request_id)
            try:
                response = self._engine.exchange(
                    frame,
                    request_id,
                    CLAUDE_ENGINE_EVENT_TIMEOUT_SECONDS,
                )
            finally:
                clear_secret_buffer(frame)
            decode_initialize_success(response, request_id)
            self._initialized = True

    def remember_ambiguous(
        self,
        binding: ClaudeStructuredBinding,
    ) -> None:
        """Retain the exact target after an ambiguous first install."""
        with self._session_lock:
            self._ambiguous_bootstrap = binding

    def install(
        self,
        frame: ClaudeStructuredProtectedFrame,
        session: ClaudeStructuredSession | None,
    ) -> tuple[ClaudeStructuredSession, ClaudeStructuredInstallReceipt]:
        """Install a fresh projection for the retained target binding."""
        with self._engine_lock, self._session_lock:
            if session is None:
                if frame.protected_binding != self._ambiguous_bootstrap:
                    raise ClaudeStructuredError(
                        ClaudeStructuredFailure.AUTHORITY_MISMATCH
                    )
                return self.bind_initial(frame)
            session.prepare_target(frame.protected_binding)
            return session, session.update_oauth(frame)

    def retain_failure(
        self,
        channel: ClaudeProtectedHostChannel,
        frame: ClaudeStructuredProtectedFrame,
        error: ClaudeStructuredError,
    ) -> bool:
        """Retain every ambiguous install unless the engine has exited."""
        frame.close_protected_frame()
        if error.code is ClaudeStructuredFailure.PROCESS_EXITED:
            channel.close()
            return False
        channel.release_ambiguous_projection()
        return True

    def reject(
        self,
        channel: ClaudeProtectedHostChannel,
        frame: ClaudeStructuredProtectedFrame,
        session: ClaudeStructuredSession | None,
    ) -> None:
        """Reject an untyped failure outside the protected contract."""
        frame.close_protected_frame()
        with self._session_lock:
            if session is not None:
                session.discard_uninstalled_target()
        channel.close()

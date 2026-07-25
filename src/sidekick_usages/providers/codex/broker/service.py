"""Release-gated shared Codex runtime and projection readiness."""

from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.platform.types import PeerVerifier
from sidekick_usages.providers.codex.app_server.capabilities import (
    probe_codex_capabilities,
)
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.types import (
    JsonRpcMessage,
)
from sidekick_usages.providers.codex.app_server.models import CodexExecutable
from sidekick_usages.providers.codex.broker.daemon import CodexDaemonManager
from sidekick_usages.providers.codex.broker.errors import (
    CodexBrokerError,
    codex_broker_error,
)
from sidekick_usages.providers.codex.broker.external_auth.installation import (
    install_codex_projection,
)
from sidekick_usages.providers.codex.broker.external_auth.refresh import (
    CODEX_REFRESH_ERROR_CODE,
    CODEX_REFRESH_ERROR_MESSAGE,
    codex_refresh_result,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexDaemonAuthority,
    CodexProjectionExpectation,
    CodexProjectionReceipt,
    CodexProjectionReplyLease,
)
from sidekick_usages.providers.codex.broker.ports import CodexProjection
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from sidekick_usages.providers.codex.broker.wire import CodexDaemonSession
from sidekick_usages.providers.codex.generation import codex_generation_order


class CodexSharedRuntime:
    """Own one qualified daemon connection and correlated projection."""

    def __init__(self, manager: CodexDaemonManager) -> None:
        self._manager = manager
        self._session: CodexDaemonSession | None = None
        self._authority: CodexDaemonAuthority | None = None
        self._expected: CodexProjectionExpectation | None = None
        self._receipt: CodexProjectionReceipt | None = None

    @classmethod
    def create(
        cls,
        executable: CodexExecutable,
        native_home: Path,
        *,
        environment: Mapping[str, str] | None = None,
        expected_user_id: int | None = None,
        peer_verifier: PeerVerifier | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> CodexSharedRuntime:
        """Probe the exact schema before composing the shared runtime."""
        try:
            capabilities = probe_codex_capabilities(
                executable,
                environment,
                cancelled=cancelled,
            )
        except CodexAppServerError as error:
            raise codex_broker_error(error) from None
        return cls(
            CodexDaemonManager(
                capabilities,
                native_home,
                environment=environment,
                expected_user_id=expected_user_id,
                peer_verifier=peer_verifier,
                cancelled=cancelled,
            )
        )

    @property
    def qualified(self) -> bool:
        """Return whether the exact shared-daemon connection remains live."""
        session = self._session
        return (
            session is not None
            and self._authority is not None
            and not session.closed
        )

    @property
    def ready(self) -> bool:
        """Return whether this live connection has a proven projection."""
        authority = self._authority
        receipt = self._receipt
        return (
            self.qualified
            and authority is not None
            and receipt is not None
            and receipt.socket_device == authority.control_socket.device
            and receipt.socket_inode == authority.control_socket.inode
        )

    @property
    def receipt(self) -> CodexProjectionReceipt | None:
        """Return the current secret-free projection receipt."""
        return self._receipt if self.ready else None

    def qualify(self) -> None:
        """Qualify the pinned daemon connection without changing auth."""
        try:
            self._qualify_session()
        except CodexAppServerError as error:
            self._drop_session()
            raise codex_broker_error(error) from None
        except CodexBrokerError:
            self._drop_session()
            raise

    def prepare(
        self,
        account_id: SidekickAccountId,
        provider_identity: ProviderIdentity,
        generation: AuthorityGeneration,
    ) -> CodexProjectionReceipt | None:
        """Qualify readiness before any access-token lease is opened."""
        expectation = CodexProjectionExpectation(
            account_id,
            provider_identity,
            generation,
        )
        self.qualify()
        self._expected = expectation
        receipt = self._receipt
        if receipt is not None and receipt.matches(expectation):
            return receipt
        self._receipt = None
        return None

    def install(
        self,
        projection: CodexProjection,
        *,
        deadline: float | None = None,
    ) -> CodexProjectionReceipt:
        """Install one prepared ephemeral projection and mark it ready."""
        session = self._session
        expectation = self._expected
        if session is None or session.closed or expectation is None:
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_CHANGED)
        if (
            projection.account_id != expectation.account_id
            or projection.provider_identity != expectation.provider_identity
            or not _generation_not_older(
                projection.generation,
                expectation.generation,
            )
        ):
            raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
        try:
            receipt = install_codex_projection(
                session,
                projection,
                deadline=deadline,
            )
        except CodexAppServerError as error:
            self._drop_session()
            raise codex_broker_error(error) from None
        except CodexBrokerError:
            self._receipt = None
            raise
        self._expected = CodexProjectionExpectation(
            projection.account_id,
            projection.provider_identity,
            projection.generation,
        )
        self._receipt = receipt
        return receipt

    def receive(
        self,
        *,
        timeout_seconds: float,
    ) -> JsonRpcMessage:
        """Receive one daemon message through the sole session owner."""
        session = self._session
        if session is None or session.closed or not self.ready:
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_CHANGED)
        return session.receive(timeout_seconds=timeout_seconds)

    def respond_refresh(
        self,
        request_id: int,
        reply: CodexProjectionReplyLease,
        source_generation: AuthorityGeneration,
        *,
        timeout_seconds: float,
    ) -> CodexProjectionReceipt:
        """Dispatch one correlated refresh result and advance readiness."""
        session = self._session
        authority = self._authority
        receipt = self._receipt
        if (
            session is None
            or session.closed
            or authority is None
            or receipt is None
            or receipt.account_id != reply.account_id
            or receipt.provider_identity != reply.provider_identity
            or receipt.generation != source_generation
        ):
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_CHANGED)
        result = codex_refresh_result(reply)
        try:
            session.respond(
                request_id,
                result,
                timeout_seconds=timeout_seconds,
            )
        finally:
            result.clear()
        updated = replace(
            receipt,
            generation=reply.generation,
            plan=reply.plan,
        )
        self._expected = CodexProjectionExpectation(
            reply.account_id,
            reply.provider_identity,
            reply.generation,
        )
        self._receipt = updated
        return updated

    def reject_refresh(
        self,
        request_id: int,
        *,
        timeout_seconds: float,
    ) -> None:
        """Dispatch the one fixed external-auth refresh error."""
        session = self._session
        if session is None or session.closed:
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_CHANGED)
        session.respond_error(
            request_id,
            CODEX_REFRESH_ERROR_CODE,
            CODEX_REFRESH_ERROR_MESSAGE,
            timeout_seconds=timeout_seconds,
        )

    def close(self) -> None:
        """Close the resident daemon connection and clear readiness."""
        self._drop_session()

    def _qualify_session(self) -> None:
        authority = self._manager.ensure_running()
        session = self._session
        if (
            session is not None
            and not session.closed
            and self._authority == authority
        ):
            return
        self._drop_session()
        self._session = CodexDaemonSession.open(self._manager, authority)
        self._authority = authority

    def _drop_session(self) -> None:
        session = self._session
        self._session = None
        self._authority = None
        self._expected = None
        self._receipt = None
        if session is not None:
            session.close()


def _generation_not_older(
    candidate: AuthorityGeneration,
    baseline: AuthorityGeneration,
) -> bool:
    try:
        return codex_generation_order(
            str(candidate)
        ) >= codex_generation_order(str(baseline))
    except ValueError:
        return False

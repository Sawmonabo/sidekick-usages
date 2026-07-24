"""Release-gated shared Codex runtime and projection readiness."""

from collections.abc import Mapping
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
from sidekick_usages.providers.codex.app_server.models import CodexExecutable
from sidekick_usages.providers.codex.broker.daemon import CodexDaemonManager
from sidekick_usages.providers.codex.broker.errors import (
    CodexBrokerError,
    codex_broker_error,
)
from sidekick_usages.providers.codex.broker.external_auth import (
    install_codex_projection,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexDaemonAuthority,
    CodexProjectionExpectation,
    CodexProjectionReceipt,
)
from sidekick_usages.providers.codex.broker.types import (
    CodexBrokerFailure,
    CodexProjection,
)
from sidekick_usages.providers.codex.broker.wire import CodexDaemonSession


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
    ) -> CodexSharedRuntime:
        """Probe the exact schema before composing the shared runtime."""
        try:
            capabilities = probe_codex_capabilities(executable, environment)
        except CodexAppServerError as error:
            raise codex_broker_error(error) from None
        return cls(
            CodexDaemonManager(
                capabilities,
                native_home,
                environment=environment,
                expected_user_id=expected_user_id,
                peer_verifier=peer_verifier,
            )
        )

    @property
    def ready(self) -> bool:
        """Return whether this live connection has a proven projection."""
        session = self._session
        authority = self._authority
        receipt = self._receipt
        return (
            session is not None
            and authority is not None
            and receipt is not None
            and not session.closed
            and receipt.socket_device == authority.control_socket.device
            and receipt.socket_inode == authority.control_socket.inode
        )

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
        try:
            authority = self._manager.ensure_running()
            session = self._session
            if (
                session is None
                or session.closed
                or self._authority != authority
            ):
                self._drop_session()
                session = CodexDaemonSession.open(self._manager, authority)
                self._session = session
                self._authority = authority
                self._receipt = None
            self._expected = expectation
        except CodexAppServerError as error:
            self._drop_session()
            raise codex_broker_error(error) from None
        except CodexBrokerError:
            self._drop_session()
            raise
        receipt = self._receipt
        if receipt is not None and _receipt_matches(receipt, expectation):
            return receipt
        self._receipt = None
        return None

    def install(
        self,
        projection: CodexProjection,
    ) -> CodexProjectionReceipt:
        """Install one prepared ephemeral projection and mark it ready."""
        session = self._session
        expectation = self._expected
        if session is None or session.closed or expectation is None:
            raise CodexBrokerError(CodexBrokerFailure.RUNTIME_CHANGED)
        if (
            projection.account_id != expectation.account_id
            or projection.provider_identity
            != expectation.provider_identity
            or projection.generation != expectation.generation
        ):
            raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
        try:
            receipt = install_codex_projection(session, projection)
        except CodexAppServerError as error:
            self._drop_session()
            raise codex_broker_error(error) from None
        except CodexBrokerError:
            self._receipt = None
            raise
        self._receipt = receipt
        return receipt

    def close(self) -> None:
        """Close the resident daemon connection and clear readiness."""
        self._drop_session()

    def _drop_session(self) -> None:
        session = self._session
        self._session = None
        self._authority = None
        self._expected = None
        self._receipt = None
        if session is not None:
            session.close()


def _receipt_matches(
    receipt: CodexProjectionReceipt,
    expectation: CodexProjectionExpectation,
) -> bool:
    return (
        receipt.account_id == expectation.account_id
        and receipt.provider_identity == expectation.provider_identity
        and receipt.generation == expectation.generation
    )

"""Complete CLI ownership of one coordinated stock Codex TUI."""

import os
import socket
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path
from threading import Event, RLock, Thread
from uuid import uuid4

from sidekick_usages.cli.session.launcher import ProviderSessionLauncher
from sidekick_usages.core.selection.models import safe_outcome_code
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    TurnId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import (
    ControlClient,
)
from sidekick_usages.daemon.control.protocol import FramedTransport
from sidekick_usages.daemon.models.protocol import (
    AcceptedPayload,
    CompletedPayload,
    ControlEvent,
    FailedPayload,
)
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionProof,
    ParticipantClientKind,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantNoticeKind,
    ParticipantReadyProof,
    ParticipantRegistration,
    TurnAdmission,
    TurnAdmissionState,
)
from sidekick_usages.daemon.types.protocol import (
    CompletionOutcome,
    EventKind,
)
from sidekick_usages.providers.codex.app_server.capabilities import (
    probe_codex_capabilities,
)
from sidekick_usages.providers.codex.app_server.models import CodexExecutable
from sidekick_usages.providers.codex.broker.daemon import CodexDaemonManager
from sidekick_usages.providers.codex.broker.wire import CodexDaemonSession
from sidekick_usages.providers.codex.session.errors import CodexRelayError
from sidekick_usages.providers.codex.session.models import (
    CodexRelayAdmission,
    CodexRelayAdmissionState,
    CodexRelayAuthority,
)
from sidekick_usages.providers.codex.session.quiescence import (
    CodexParticipantProofChannel,
)
from sidekick_usages.providers.codex.session.relay import CodexAdmissionRelay

_CONNECTION_GENERATION = 1
_NOTICE_THREAD_JOIN_SECONDS = 1.0
_PRIVATE_DIRECTORY_MODE = 0o700


class _CodexParticipantControl:
    """Adapt one authenticated control connection to relay operations."""

    def __init__(
        self,
        client: ControlClient,
        participant_id: ParticipantId,
        connection_generation: int,
    ) -> None:
        self._client = client
        self._participant_id = participant_id
        self._connection_generation = connection_generation
        self._lock = RLock()

    def register(
        self,
        protected_endpoint: socket.socket,
    ) -> ParticipantRegistration:
        """Register the exact CLI process as one Codex participant."""
        manifest = ParticipantManifest(
            participant_id=self._participant_id,
            provider_id=ProviderId.CODEX,
            client_kind=ParticipantClientKind.CODEX_CLI,
            capability_version=1,
            connection_generation=self._connection_generation,
        )
        payload = self._payload(
            self._client.register_participant(
                manifest,
                protected_endpoint=protected_endpoint,
            ),
            EventKind.PARTICIPANT_REGISTERED,
        )
        if (
            not isinstance(payload, ParticipantRegistration)
            or payload.participant_id != self._participant_id
            or payload.provider_id is not ProviderId.CODEX
            or payload.connection_generation != self._connection_generation
        ):
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        return payload

    def begin(self, turn_id: TurnId) -> CodexRelayAdmission:
        """Translate one exact supervisor turn admission."""
        with self._lock:
            payload = self._payload(
                self._client.begin_turn(
                    self._participant_id,
                    self._connection_generation,
                    turn_id,
                ),
                EventKind.TURN_ADMISSION,
            )
        if (
            not isinstance(payload, TurnAdmission)
            or payload.participant_id != self._participant_id
            or payload.turn_id != turn_id
        ):
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        if payload.state is TurnAdmissionState.QUEUED:
            return CodexRelayAdmission(
                turn_id=turn_id,
                state=CodexRelayAdmissionState.QUEUED,
                authority=None,
            )
        if (
            payload.epoch is None
            or payload.account_id is None
            or payload.generation is None
        ):
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        return CodexRelayAdmission(
            turn_id=turn_id,
            state=CodexRelayAdmissionState.ADMITTED,
            authority=CodexRelayAuthority(
                account_id=payload.account_id,
                generation=payload.generation,
                epoch=payload.epoch,
            ),
        )

    def recheck(self, admission: CodexRelayAdmission) -> None:
        """Require an idempotent admission immediately before transmission."""
        if self.begin(admission.turn_id) != admission:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)

    def end(self, turn_id: TurnId) -> None:
        """Close one naturally terminal exact turn lease."""
        with self._lock:
            events = self._client.end_turn(
                self._participant_id,
                self._connection_generation,
                turn_id,
            )
            self._completed(events)

    def ready(self, target: CodexRelayAuthority) -> None:
        """Acknowledge one target only after provider-local readiness."""
        proof = ParticipantReadyProof(
            account_id=target.account_id,
            generation=target.generation,
            epoch=target.epoch,
        )
        with self._lock:
            self._completed(
                self._client.participant_ready(
                    self._participant_id,
                    self._connection_generation,
                    proof,
                )
            )

    def adopted(
        self,
        turn_id: TurnId,
        target: CodexRelayAuthority,
    ) -> None:
        """Publish first-real-turn adoption for one opened target."""
        proof = ParticipantAdoptionProof(
            turn_id=turn_id,
            account_id=target.account_id,
            generation=target.generation,
            epoch=target.epoch,
        )
        with self._lock:
            self._completed(
                self._client.participant_adopted(
                    self._participant_id,
                    self._connection_generation,
                    proof,
                )
            )

    def _completed(self, events: Iterator[ControlEvent]) -> None:
        payload = self._payload(events, EventKind.COMPLETED)
        if (
            not isinstance(payload, CompletedPayload)
            or payload.operation_id is not None
            or payload.outcome is not CompletionOutcome.SUCCEEDED
        ):
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)

    @staticmethod
    def _payload(
        events: Iterator[ControlEvent],
        expected_kind: EventKind,
    ) -> object:
        try:
            event = next(events)
        except StopIteration:
            raise CodexRelayError(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            ) from None
        if event.kind is EventKind.FAILED:
            payload = event.payload
            if isinstance(payload, FailedPayload):
                code = safe_outcome_code(payload.code)
                try:
                    selection_code = (
                        SelectionCode.SELECTION_RECOVERY_REQUIRED
                        if code is None
                        else SelectionCode(code)
                    )
                except ValueError:
                    selection_code = SelectionCode.SELECTION_RECOVERY_REQUIRED
                raise CodexRelayError(selection_code)
        if event.kind is not expected_kind:
            raise CodexRelayError(SelectionCode.SELECTION_RECOVERY_REQUIRED)
        try:
            next(events)
        except StopIteration:
            return event.payload
        raise CodexRelayError(SelectionCode.SELECTION_RECOVERY_REQUIRED)


class CodexSessionRuntime:
    """Own one qualified relay and registered participant lifetime."""

    def __init__(
        self,
        socket_path: Path,
        supervisor_socket: Path,
        manager: CodexDaemonManager,
    ) -> None:
        if (
            not socket_path.is_absolute()
            or not supervisor_socket.is_absolute()
        ):
            raise ValueError("Codex runtime paths must be absolute.")
        self._socket_path = socket_path
        self._supervisor_socket = supervisor_socket
        self._manager = manager
        self._participant_id = ParticipantId(str(uuid4()))
        self._connection_generation = _CONNECTION_GENERATION
        self._closing = Event()
        self._relay: CodexAdmissionRelay | None = None
        self._readiness_session: CodexDaemonSession | None = None
        self._control_client: ControlClient | None = None
        self._subscription_client: ControlClient | None = None
        self._proof_channel: CodexParticipantProofChannel | None = None
        self._notice_thread: Thread | None = None
        self._opened_once = False

    @classmethod
    def create(
        cls,
        executable: CodexExecutable,
        codex_home: Path,
        socket_path: Path,
        supervisor_socket: Path,
        *,
        environment: Mapping[str, str] | None = None,
        expected_user_id: int | None = None,
    ) -> CodexSessionRuntime:
        """Probe the release and compose one read-only resident attachment."""
        capabilities = probe_codex_capabilities(executable, environment)
        return cls(
            socket_path,
            supervisor_socket,
            CodexDaemonManager(
                capabilities,
                codex_home,
                environment=environment,
                expected_user_id=expected_user_id,
            ),
        )

    @property
    def socket_path(self) -> Path:
        """Return the stable owner-only stock-TUI relay endpoint."""
        return self._socket_path

    def open(self) -> None:
        """Qualify, start, register, and subscribe one participant."""
        if self._opened_once:
            raise RuntimeError("Codex session runtime is already open.")
        self._opened_once = True
        self._prepare_socket_directory()
        authority = self._manager.attach_running()
        readiness_session = CodexDaemonSession.open(self._manager, authority)
        control_client: ControlClient | None = None
        subscription_client: ControlClient | None = None
        relay: CodexAdmissionRelay | None = None
        proof_channel: CodexParticipantProofChannel | None = None
        supervisor_endpoint: socket.socket | None = None
        try:
            self._manager.session_config.qualify(
                readiness_session,
                self._manager.session_config_version,
                session_schema_supported=(
                    self._manager.session_schema_supported
                ),
            )
            control_client = ControlClient.connect(self._supervisor_socket)
            subscription_client = ControlClient.connect(
                self._supervisor_socket
            )
            participant = _CodexParticipantControl(
                control_client,
                self._participant_id,
                self._connection_generation,
            )
            relay = CodexAdmissionRelay.open(
                self._socket_path,
                self._manager.connect(authority),
                participant,
                participant,
                readiness_session,
                turn_id_factory=lambda: TurnId(str(uuid4())),
            )
            self._manager.revalidate(authority)
            proof_channel, supervisor_endpoint = (
                CodexParticipantProofChannel.create(FramedTransport)
            )
            participant.register(supervisor_endpoint)
            supervisor_endpoint = None
            notices = subscription_client.subscribe_participant(
                self._participant_id,
                self._connection_generation,
            )
            self._require_subscription_acceptance(notices)
            try:
                initial_event = next(notices)
            except StopIteration:
                raise CodexRelayError(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                ) from None
            self._apply_notice(
                relay,
                proof_channel,
                self._require_notice(initial_event),
            )
            self._relay = relay
            self._readiness_session = readiness_session
            self._control_client = control_client
            self._subscription_client = subscription_client
            self._proof_channel = proof_channel
            thread = Thread(
                target=self._consume_notices,
                args=(notices,),
                daemon=True,
                name="codex-participant-notices",
            )
            self._notice_thread = thread
            thread.start()
        except BaseException:
            if supervisor_endpoint is not None:
                supervisor_endpoint.close()
            if proof_channel is not None:
                proof_channel.close()
            if relay is not None:
                relay.close()
            if subscription_client is not None:
                subscription_client.close()
            if control_client is not None:
                control_client.close()
            readiness_session.close()
            raise

    def close(self) -> None:
        """Close only resources owned by this participant session."""
        self._closing.set()
        failures: list[BaseException] = []
        resources = (
            self._subscription_client,
            self._proof_channel,
            self._relay,
            self._control_client,
            self._readiness_session,
        )
        for resource in resources:
            if resource is None:
                continue
            try:
                resource.close()
            except BaseException as error:
                failures.append(error)
        thread = self._notice_thread
        if thread is not None:
            thread.join(timeout=_NOTICE_THREAD_JOIN_SECONDS)
        self._relay = None
        self._readiness_session = None
        self._control_client = None
        self._subscription_client = None
        self._proof_channel = None
        self._notice_thread = None
        if thread is not None and thread.is_alive():
            failures.append(
                RuntimeError("Codex participant subscription did not close.")
            )
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup(
                "Codex participant resources did not all close.",
                failures,
            )

    def _consume_notices(self, notices: Iterator[ControlEvent]) -> None:
        relay = self._relay
        proof_channel = self._proof_channel
        if relay is None or proof_channel is None:
            return
        try:
            for event in notices:
                self._apply_notice(
                    relay,
                    proof_channel,
                    self._require_notice(event),
                )
            if not self._closing.is_set():
                relay.refuse_admission(
                    SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
                relay.discard_quiescence()
        except BaseException as error:
            if not self._closing.is_set():
                relay.refuse_admission(
                    error.code
                    if isinstance(error, CodexRelayError)
                    else SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
                relay.discard_quiescence()

    @staticmethod
    def _apply_notice(
        relay: CodexAdmissionRelay,
        proof_channel: CodexParticipantProofChannel,
        notice: ParticipantNotice,
    ) -> None:
        if notice.kind is ParticipantNoticeKind.READY:
            if (
                notice.target_account_id is None
                or notice.target_generation is None
            ):
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            relay.mark_ready(
                CodexRelayAuthority(
                    account_id=notice.target_account_id,
                    generation=notice.target_generation,
                    epoch=notice.epoch,
                ),
                relay.loaded_threads_snapshot,
            )
        elif notice.kind is ParticipantNoticeKind.OPEN:
            relay.open_epoch(notice.epoch)
            relay.discard_quiescence()
        elif notice.kind is ParticipantNoticeKind.PREPARE:
            relay.prepare_admission()
            proof_channel.serve_selection(relay, notice.epoch)
        elif notice.kind is ParticipantNoticeKind.STATUS:
            relay.enter_recovery(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
                if notice.code is None
                else notice.code,
                notice.epoch,
            )
        else:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)

    def _prepare_socket_directory(self) -> None:
        parent = self._socket_path.parent
        parent.mkdir(
            parents=True,
            exist_ok=True,
            mode=_PRIVATE_DIRECTORY_MODE,
        )
        try:
            status = parent.lstat()
        except OSError:
            raise CodexRelayError(
                SelectionCode.PARTICIPANT_UNREACHABLE
            ) from None
        if (
            not stat.S_ISDIR(status.st_mode)
            or status.st_uid != os.geteuid()
            or status.st_mode & 0o777 != _PRIVATE_DIRECTORY_MODE
        ):
            raise CodexRelayError(SelectionCode.PARTICIPANT_UNREACHABLE)

    def _require_notice(self, event: ControlEvent) -> ParticipantNotice:
        notice = event.payload
        if (
            event.kind is not EventKind.PARTICIPANT_NOTICE
            or not isinstance(notice, ParticipantNotice)
            or notice.participant_id != self._participant_id
            or notice.provider_id is not ProviderId.CODEX
        ):
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        return notice

    @staticmethod
    def _require_subscription_acceptance(
        notices: Iterator[ControlEvent],
    ) -> None:
        try:
            accepted = next(notices)
        except StopIteration:
            raise CodexRelayError(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            ) from None
        if (
            accepted.kind is not EventKind.ACCEPTED
            or not isinstance(accepted.payload, AcceptedPayload)
            or accepted.payload.operation_id is not None
        ):
            raise CodexRelayError(SelectionCode.SELECTION_RECOVERY_REQUIRED)


class CodexCliSession:
    """Launch one stock Codex TUI through one retained participant relay."""

    def __init__(
        self,
        launcher: ProviderSessionLauncher,
        runtime: CodexSessionRuntime,
        *,
        codex_home: Path,
    ) -> None:
        if not codex_home.is_absolute():
            raise ValueError("The neutral Codex home must be absolute.")
        self._launcher = launcher
        self._runtime = runtime
        self._codex_home = codex_home

    def run(self, arguments: tuple[str, ...]) -> int:
        """Run exactly one stock TUI and return its natural exit status."""
        spec = self._launcher.plan_codex_remote(
            arguments,
            socket_path=self._runtime.socket_path,
            codex_home=self._codex_home,
        )
        child = self._launcher.prepare_child(spec)
        try:
            self._runtime.open()
        except BaseException:
            child.cancel()
            raise
        result = 0
        failure: BaseException | None = None
        try:
            result = child.run()
        except BaseException as error:
            failure = error
        try:
            self._runtime.close()
        except BaseException as cleanup_error:
            if failure is None:
                raise
            raise BaseExceptionGroup(
                "Codex session and cleanup both failed.",
                [failure, cleanup_error],
            ) from None
        if failure is not None:
            raise failure
        return result

"""Complete CLI ownership of one coordinated stock Codex TUI."""

import os
import socket
import stat
from collections.abc import Iterator, Mapping
from contextlib import suppress
from pathlib import Path
from threading import Event, RLock, Thread
from uuid import uuid4

from sidekick_usages.cli.session.control import (
    ParticipantControl,
    SessionParticipantError,
    participant_reattach_delay,
)
from sidekick_usages.cli.session.launcher import ProviderSessionLauncher
from sidekick_usages.core.selection.types import (
    ParticipantId,
    SelectionCode,
    TurnId,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import (
    ControlClient,
)
from sidekick_usages.daemon.control.protocol import (
    ConnectionClosedError,
    FramedTransport,
)
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionProof,
    ParticipantClientKind,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantNoticeKind,
    ParticipantReadyProof,
    TurnAdmission,
    TurnAdmissionState,
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
    CodexParticipantProofError,
)
from sidekick_usages.providers.codex.session.relay import CodexAdmissionRelay

_CONNECTION_GENERATION = 1
_NOTICE_THREAD_JOIN_SECONDS = 1.0
_PRIVATE_DIRECTORY_MODE = 0o700
_CODEX_REATTACH_ERRORS = (
    CodexParticipantProofError,
    CodexRelayError,
    ConnectionClosedError,
    OSError,
    SessionParticipantError,
)


class _CodexParticipantControl:
    """Retain relay admission while supervisor control is replaced."""

    def __init__(
        self,
        control: ParticipantControl,
        connection_generation: int,
    ) -> None:
        self._lock = RLock()
        self._control: ParticipantControl | None = control
        self._connection_generation = connection_generation
        self._turn_generations: dict[TurnId, int] = {}

    def replace(
        self,
        control: ParticipantControl,
        connection_generation: int,
    ) -> None:
        """Install one newer connection without replacing the relay."""
        with self._lock:
            if connection_generation <= self._connection_generation:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            previous = self._control
            self._control = control
            self._connection_generation = connection_generation
        if previous is not None:
            with suppress(Exception):
                previous.close()

    def disconnect(self, connection_generation: int) -> None:
        """Detach only the failed connection generation."""
        with self._lock:
            if connection_generation != self._connection_generation:
                return
            previous = self._control
            self._control = None
        if previous is not None:
            with suppress(Exception):
                previous.close()

    def begin(self, turn_id: TurnId) -> CodexRelayAdmission:
        """Translate one exact supervisor turn admission."""
        with self._lock:
            control = self._control
            if control is None:
                return CodexRelayAdmission(
                    turn_id=turn_id,
                    state=CodexRelayAdmissionState.QUEUED,
                    authority=None,
                )
            try:
                admission = control.begin(turn_id)
            except SessionParticipantError as error:
                raise CodexRelayError(error.code) from None
            translated = self._translate_admission(turn_id, admission)
            if translated.state is CodexRelayAdmissionState.ADMITTED:
                self._turn_generations[turn_id] = self._connection_generation
            return translated

    def recheck(self, admission: CodexRelayAdmission) -> None:
        """Require an idempotent admission immediately before transmission."""
        if self.begin(admission.turn_id) != admission:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)

    def end(self, turn_id: TurnId) -> None:
        """Close one naturally terminal exact turn lease."""
        with self._lock:
            turn_generation = self._turn_generations.get(turn_id)
            if turn_generation is None:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            control = self._control
            if (
                control is None
                or turn_generation != self._connection_generation
            ):
                self._turn_generations.pop(turn_id)
                return
            try:
                control.end(turn_id)
            except SessionParticipantError as error:
                raise CodexRelayError(error.code) from None
            self._turn_generations.pop(turn_id)

    def ready(self, target: CodexRelayAuthority) -> None:
        """Acknowledge one target only after provider-local readiness."""
        proof = ParticipantReadyProof(
            account_id=target.account_id,
            generation=target.generation,
            epoch=target.epoch,
        )
        with self._lock:
            control = self._require_control()
            try:
                control.ready(proof)
            except SessionParticipantError as error:
                raise CodexRelayError(error.code) from None

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
            control = self._require_control()
            try:
                control.adopted(proof)
            except SessionParticipantError as error:
                raise CodexRelayError(error.code) from None

    def close(self) -> None:
        """Close both supervisor connections."""
        with self._lock:
            control = self._control
            self._control = None
        if control is not None:
            control.close()

    def _require_control(self) -> ParticipantControl:
        control = self._control
        if control is None:
            raise CodexRelayError(SelectionCode.SELECTION_RECOVERY_REQUIRED)
        return control

    @staticmethod
    def _translate_admission(
        turn_id: TurnId,
        admission: TurnAdmission,
    ) -> CodexRelayAdmission:
        if admission.state is TurnAdmissionState.QUEUED:
            return CodexRelayAdmission(
                turn_id=turn_id,
                state=CodexRelayAdmissionState.QUEUED,
                authority=None,
            )
        if (
            admission.epoch is None
            or admission.account_id is None
            or admission.generation is None
        ):
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        return CodexRelayAdmission(
            turn_id=turn_id,
            state=CodexRelayAdmissionState.ADMITTED,
            authority=CodexRelayAuthority(
                account_id=admission.account_id,
                generation=admission.generation,
                epoch=admission.epoch,
            ),
        )


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
        self._state_lock = RLock()
        self._relay: CodexAdmissionRelay | None = None
        self._readiness_session: CodexDaemonSession | None = None
        self._participant: _CodexParticipantControl | None = None
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
        control: ParticipantControl | None = None
        participant: _CodexParticipantControl | None = None
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
            control = self._connect_control(
                self._connection_generation,
            )
            participant = _CodexParticipantControl(
                control,
                self._connection_generation,
            )
            control = None
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
            active_control = participant._require_control()
            self._register_control(active_control, supervisor_endpoint)
            supervisor_endpoint = None
            notices, initial = self._prepare_notices(active_control)
            self._apply_notice(
                relay,
                proof_channel,
                initial,
            )
            self._relay = relay
            self._readiness_session = readiness_session
            self._participant = participant
            self._proof_channel = proof_channel
            participant = None
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
            if participant is not None:
                participant.close()
            elif control is not None:
                control.close()
            readiness_session.close()
            raise

    def close(self) -> None:
        """Close only resources owned by this participant session."""
        self._closing.set()
        failures: list[BaseException] = []
        with self._state_lock:
            resources = (
                self._participant,
                self._proof_channel,
                self._relay,
                self._readiness_session,
            )
            thread = self._notice_thread
            self._relay = None
            self._readiness_session = None
            self._participant = None
            self._proof_channel = None
            self._notice_thread = None
        for resource in resources:
            if resource is None:
                continue
            try:
                resource.close()
            except BaseException as error:
                failures.append(error)
        if thread is not None:
            thread.join(timeout=_NOTICE_THREAD_JOIN_SECONDS)
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

    def _consume_notices(
        self,
        notices: Iterator[ParticipantNotice],
    ) -> None:
        with self._state_lock:
            relay = self._relay
            participant = self._participant
            proof_channel = self._proof_channel
            generation = self._connection_generation
        if relay is None or participant is None or proof_channel is None:
            return
        while not self._closing.is_set():
            try:
                for notice in notices:
                    self._apply_notice(relay, proof_channel, notice)
            except _CODEX_REATTACH_ERRORS:
                pass
            except BaseException as error:
                relay.refuse_admission(
                    error.code
                    if isinstance(error, CodexRelayError)
                    else SelectionCode.SELECTION_RECOVERY_REQUIRED
                )
                relay.discard_quiescence()
                return
            if self._closing.is_set():
                return
            participant.disconnect(generation)
            proof_channel.close()
            relay.discard_quiescence()
            replacement = self._reattach(participant, relay)
            if replacement is None:
                return
            notices, proof_channel, generation = replacement

    def _reattach(
        self,
        participant: _CodexParticipantControl,
        relay: CodexAdmissionRelay,
    ) -> (
        tuple[
            Iterator[ParticipantNotice],
            CodexParticipantProofChannel,
            int,
        ]
        | None
    ):
        attempt = 0
        while not self._closing.wait(participant_reattach_delay(attempt)):
            try:
                return self._reattach_once(participant, relay)
            except _CODEX_REATTACH_ERRORS:
                attempt += 1
        return None

    def _reattach_once(
        self,
        participant: _CodexParticipantControl,
        relay: CodexAdmissionRelay,
    ) -> tuple[
        Iterator[ParticipantNotice],
        CodexParticipantProofChannel,
        int,
    ]:
        with self._state_lock:
            self._connection_generation += 1
            generation = self._connection_generation
        control: ParticipantControl | None = None
        proof_channel: CodexParticipantProofChannel | None = None
        supervisor_endpoint: socket.socket | None = None
        try:
            control = self._connect_control(generation)
            proof_channel, supervisor_endpoint = (
                CodexParticipantProofChannel.create(FramedTransport)
            )
            self._register_control(control, supervisor_endpoint)
            supervisor_endpoint = None
            notices, initial = self._prepare_notices(control)
            with self._state_lock:
                if self._closing.is_set():
                    raise ConnectionClosedError(
                        "The Codex participant is closing."
                    )
                previous_channel = self._proof_channel
                participant.replace(control, generation)
                control = None
                self._proof_channel = proof_channel
                active_channel = proof_channel
                proof_channel = None
            if previous_channel is not None:
                with suppress(Exception):
                    previous_channel.close()
            try:
                self._apply_notice(relay, active_channel, initial)
            except BaseException:
                participant.disconnect(generation)
                with self._state_lock:
                    if self._proof_channel is active_channel:
                        self._proof_channel = None
                active_channel.close()
                raise
            return notices, active_channel, generation
        finally:
            if supervisor_endpoint is not None:
                supervisor_endpoint.close()
            if proof_channel is not None:
                proof_channel.close()
            if control is not None:
                control.close()

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

    def _connect_control(
        self,
        connection_generation: int,
    ) -> ParticipantControl:
        action = ControlClient.connect(self._supervisor_socket)
        subscription: ControlClient | None = None
        try:
            subscription = ControlClient.connect(self._supervisor_socket)
        except BaseException:
            action.close()
            raise
        manifest = ParticipantManifest(
            participant_id=self._participant_id,
            provider_id=ProviderId.CODEX,
            client_kind=ParticipantClientKind.CODEX_CLI,
            capability_version=1,
            connection_generation=connection_generation,
        )
        return ParticipantControl(action, subscription, manifest)

    @staticmethod
    def _register_control(
        control: ParticipantControl,
        protected_endpoint: socket.socket,
    ) -> None:
        try:
            control.register(protected_endpoint)
        except SessionParticipantError as error:
            raise CodexRelayError(error.code) from None

    @staticmethod
    def _prepare_notices(
        control: ParticipantControl,
    ) -> tuple[Iterator[ParticipantNotice], ParticipantNotice]:
        notices = control.notices()
        try:
            initial = next(notices)
        except SessionParticipantError as error:
            raise CodexRelayError(error.code) from None
        except StopIteration:
            raise CodexRelayError(
                SelectionCode.SELECTION_RECOVERY_REQUIRED
            ) from None
        return notices, initial


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

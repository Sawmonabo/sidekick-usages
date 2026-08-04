"""Complete CLI ownership of one coordinated stock Codex TUI."""

import errno
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
    ControlConnectionAttempt,
    ServiceCompatibilityError,
    UnexpectedServiceEventError,
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
)
from sidekick_usages.providers.codex.session.relay import CodexAdmissionRelay

_CONNECTION_GENERATION = 1
_NOTICE_THREAD_JOIN_SECONDS = 1.0
_PRIVATE_DIRECTORY_MODE = 0o700
_REATTACH_CONNECT_TIMEOUT_SECONDS = 0.5
_CODEX_UNAVAILABLE_ERRNOS = frozenset(
    {
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ENOENT,
        errno.ENOTCONN,
        errno.EPIPE,
        errno.ETIMEDOUT,
    }
)
_CODEX_CONNECTION_ERRORS = (
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionClosedError,
    ConnectionRefusedError,
    ConnectionResetError,
    FileNotFoundError,
    TimeoutError,
)


def _codex_control_unavailable(error: BaseException) -> bool:
    """Return whether one failure proves only local control absence."""
    if isinstance(
        error,
        (ServiceCompatibilityError, UnexpectedServiceEventError),
    ):
        return False
    if isinstance(error, _CODEX_CONNECTION_ERRORS):
        return True
    return (
        isinstance(error, OSError) and error.errno in _CODEX_UNAVAILABLE_ERRNOS
    )


class _CodexAttachmentAttempt:
    """Own one unpublished reconnect attempt for cancellable shutdown."""

    def __init__(self) -> None:
        self.action_attempt: ControlConnectionAttempt | None = None
        self.action_client: ControlClient | None = None
        self.control: ParticipantControl | None = None
        self.proof_channel: CodexParticipantProofChannel | None = None
        self.subscription_attempt: ControlConnectionAttempt | None = None
        self.subscription_client: ControlClient | None = None
        self.supervisor_endpoint: socket.socket | None = None

    def close(self) -> None:
        """Close every resource not transferred to the live runtime."""
        resources = (
            self.supervisor_endpoint,
            self.proof_channel,
            self.control,
            self.subscription_client,
            self.subscription_attempt,
            self.action_client,
            self.action_attempt,
        )
        self.action_attempt = None
        self.action_client = None
        self.supervisor_endpoint = None
        self.proof_channel = None
        self.subscription_attempt = None
        self.subscription_client = None
        self.control = None
        for resource in resources:
            if resource is not None:
                with suppress(Exception):
                    resource.close()


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
        self._turn_admissions: dict[TurnId, TurnAdmission] = {}
        self._active_turns: set[TurnId] = set()
        self._completed_turns: set[TurnId] = set()

    def replace(
        self,
        control: ParticipantControl,
        connection_generation: int,
    ) -> None:
        """Install one newer connection without replacing the relay."""
        with self._lock:
            if connection_generation <= self._connection_generation:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            self._resume_turns(control)
            previous = self._control
            self._control = control
            self._connection_generation = connection_generation
        if previous is not None:
            with suppress(Exception):
                previous.close()

    def prepare_replacement(
        self,
        control: ParticipantControl,
        connection_generation: int,
    ) -> None:
        """Reconstruct old leases before publishing a new subscription."""
        with self._lock:
            if connection_generation <= self._connection_generation:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            self._resume_turns(control)

    def disconnect(self, connection_generation: int) -> None:
        """Detach only the failed connection generation."""
        with self._lock:
            if connection_generation != self._connection_generation:
                return
            previous = self._control
            self._control = None
            staged = set(self._turn_admissions).difference(
                self._active_turns,
                self._completed_turns,
            )
            for turn_id in staged:
                self._turn_admissions.pop(turn_id)
        if previous is not None:
            with suppress(Exception):
                previous.close()

    def begin(self, turn_id: TurnId) -> CodexRelayAdmission:
        """Translate one exact supervisor turn admission."""
        failed: ParticipantControl | None = None
        with self._lock:
            control = self._control
            if control is None:
                return self._queued_admission(turn_id)
            try:
                admission = control.begin(turn_id)
            except SessionParticipantError as error:
                raise CodexRelayError(error.code) from None
            except BaseException as error:
                if not _codex_control_unavailable(error):
                    raise
                if self._control is control:
                    self._control = None
                    failed = control
                admission = None
            if admission is None:
                translated = self._queued_admission(turn_id)
            else:
                translated = self._translate_admission(turn_id, admission)
                if translated.state is CodexRelayAdmissionState.ADMITTED:
                    self._turn_admissions[turn_id] = admission
        if failed is not None:
            with suppress(Exception):
                failed.close()
        return translated

    def recheck(self, admission: CodexRelayAdmission) -> bool:
        """Require an idempotent admission immediately before transmission."""
        rechecked = self.begin(admission.turn_id)
        if rechecked.state is CodexRelayAdmissionState.QUEUED:
            with self._lock:
                self._turn_admissions.pop(admission.turn_id, None)
            return False
        if rechecked != admission:
            raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
        with self._lock:
            self._active_turns.add(admission.turn_id)
        return True

    def end(self, turn_id: TurnId) -> None:
        """Close one naturally terminal exact turn lease."""
        failed: ParticipantControl | None = None
        with self._lock:
            if turn_id not in self._active_turns:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            self._active_turns.remove(turn_id)
            self._completed_turns.add(turn_id)
            control = self._control
            if control is None:
                return
            try:
                control.end(turn_id)
            except SessionParticipantError as error:
                raise CodexRelayError(error.code) from None
            except BaseException as error:
                if not _codex_control_unavailable(error):
                    raise
                if self._control is control:
                    self._control = None
                    failed = control
            else:
                self._forget_turn(turn_id)
        if failed is not None:
            with suppress(Exception):
                failed.close()

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
            self._turn_admissions.clear()
            self._active_turns.clear()
            self._completed_turns.clear()
        if control is not None:
            control.close()

    def _resume_turns(self, control: ParticipantControl) -> None:
        for turn_id in tuple(sorted(self._turn_admissions.keys())):
            admission = self._turn_admissions[turn_id]
            try:
                resumed = control.resume(admission)
            except SessionParticipantError as error:
                raise CodexRelayError(error.code) from None
            if resumed != admission:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            if turn_id in self._completed_turns:
                try:
                    control.end(turn_id)
                except SessionParticipantError as error:
                    raise CodexRelayError(error.code) from None
                self._forget_turn(turn_id)

    def _forget_turn(self, turn_id: TurnId) -> None:
        self._turn_admissions.pop(turn_id, None)
        self._active_turns.discard(turn_id)
        self._completed_turns.discard(turn_id)

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

    @staticmethod
    def _queued_admission(turn_id: TurnId) -> CodexRelayAdmission:
        return CodexRelayAdmission(
            turn_id=turn_id,
            state=CodexRelayAdmissionState.QUEUED,
            authority=None,
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
        self._attachment_attempt: _CodexAttachmentAttempt | None = None
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
                self._attachment_attempt,
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
            self._attachment_attempt = None
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
            except BaseException as error:
                if not _codex_control_unavailable(error):
                    self._fail_reconnect(relay, error)
                    return
            if self._closing.is_set():
                return
            participant.disconnect(generation)
            proof_channel.close()
            relay.discard_quiescence()
            try:
                replacement = self._reattach(participant, relay)
            except BaseException as error:
                self._fail_reconnect(relay, error)
                return
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
            except BaseException as error:
                if not _codex_control_unavailable(error):
                    raise
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
            if self._closing.is_set():
                raise ConnectionClosedError(
                    "The Codex participant is closing."
                )
            self._connection_generation += 1
            generation = self._connection_generation
            if self._attachment_attempt is not None:
                raise CodexRelayError(SelectionCode.AUTHORITY_PROOF_FAILED)
            attempt = _CodexAttachmentAttempt()
            self._attachment_attempt = attempt
        try:
            control, proof_channel, notices, initial = self._connect_attempt(
                attempt,
                participant,
                generation,
            )
            participant.replace(control, generation)
            with self._state_lock:
                self._retain_attempt(attempt)
                previous_channel = self._proof_channel
                self._proof_channel = proof_channel
                attempt.control = None
                attempt.proof_channel = None
                self._attachment_attempt = None
                active_channel = proof_channel
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
            with self._state_lock:
                if self._attachment_attempt is attempt:
                    self._attachment_attempt = None
            attempt.close()

    def _connect_attempt(
        self,
        attempt: _CodexAttachmentAttempt,
        participant: _CodexParticipantControl,
        generation: int,
    ) -> tuple[
        ParticipantControl,
        CodexParticipantProofChannel,
        Iterator[ParticipantNotice],
        ParticipantNotice,
    ]:
        control = self._connect_replacement_control(attempt, generation)
        proof_channel, supervisor_endpoint = (
            CodexParticipantProofChannel.create(FramedTransport)
        )
        with self._state_lock:
            attempt.proof_channel = proof_channel
            attempt.supervisor_endpoint = supervisor_endpoint
            self._retain_attempt(attempt)
        self._register_control(control, supervisor_endpoint)
        with self._state_lock:
            self._retain_attempt(attempt)
            attempt.supervisor_endpoint = None
        participant.prepare_replacement(control, generation)
        notices, initial = self._prepare_notices(control)
        return control, proof_channel, notices, initial

    def _connect_replacement_control(
        self,
        attempt: _CodexAttachmentAttempt,
        generation: int,
    ) -> ParticipantControl:
        action = self._connect_attempt_client(attempt, subscription=False)
        subscription = self._connect_attempt_client(
            attempt,
            subscription=True,
        )
        control = ParticipantControl(
            action,
            subscription,
            self._participant_manifest(generation),
        )
        with self._state_lock:
            attempt.control = control
            self._retain_attempt(attempt)
            attempt.action_client = None
            attempt.subscription_client = None
        return control

    def _connect_attempt_client(
        self,
        attempt: _CodexAttachmentAttempt,
        *,
        subscription: bool,
    ) -> ControlClient:
        pending = ControlConnectionAttempt()
        with self._state_lock:
            if subscription:
                attempt.subscription_attempt = pending
            else:
                attempt.action_attempt = pending
            self._retain_attempt(attempt)
        client = pending.connect(
            self._supervisor_socket,
            connect_timeout_seconds=_REATTACH_CONNECT_TIMEOUT_SECONDS,
        )
        with self._state_lock:
            if subscription:
                attempt.subscription_client = client
            else:
                attempt.action_client = client
            self._retain_attempt(attempt)
            pending.release()
            if subscription:
                attempt.subscription_attempt = None
            else:
                attempt.action_attempt = None
        return client

    def _retain_attempt(self, attempt: _CodexAttachmentAttempt) -> None:
        if self._closing.is_set() or self._attachment_attempt is not attempt:
            raise ConnectionClosedError("The Codex participant is closing.")

    @staticmethod
    def _fail_reconnect(
        relay: CodexAdmissionRelay,
        error: BaseException,
    ) -> None:
        code = (
            error.code
            if isinstance(error, (CodexRelayError, SessionParticipantError))
            else SelectionCode.SELECTION_RECOVERY_REQUIRED
        )
        relay.refuse_admission(code)
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

    def _connect_control(
        self,
        connection_generation: int,
    ) -> ParticipantControl:
        action = ControlClient.connect(
            self._supervisor_socket,
            connect_timeout_seconds=_REATTACH_CONNECT_TIMEOUT_SECONDS,
        )
        subscription: ControlClient | None = None
        try:
            subscription = ControlClient.connect(
                self._supervisor_socket,
                connect_timeout_seconds=_REATTACH_CONNECT_TIMEOUT_SECONDS,
            )
        except BaseException:
            action.close()
            raise
        return ParticipantControl(
            action,
            subscription,
            self._participant_manifest(connection_generation),
        )

    def _participant_manifest(
        self,
        connection_generation: int,
    ) -> ParticipantManifest:
        return ParticipantManifest(
            participant_id=self._participant_id,
            provider_id=ProviderId.CODEX,
            client_kind=ParticipantClientKind.CODEX_CLI,
            capability_version=1,
            connection_generation=connection_generation,
        )

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

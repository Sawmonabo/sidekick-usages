"""Replaceable supervisor attachment for one retained Claude engine."""

import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Protocol

from sidekick_usages.cli.session.control import ParticipantControl
from sidekick_usages.core.selection.types import ParticipantId, TurnId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.daemon.control.client import ControlClient
from sidekick_usages.daemon.selection.models import (
    ParticipantAdoptionProof,
    ParticipantClientKind,
    ParticipantManifest,
    ParticipantNotice,
    ParticipantReadyProof,
    ParticipantRegistration,
    TurnAdmission,
)
from sidekick_usages.providers.claude.structured.data_plane import (
    ClaudeProtectedHostChannel,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredBinding,
)

_REPORTER_JOIN_SECONDS = 2.0


class ClaudeParticipantControl:
    """Participant operations consumed by one Claude runtime."""

    def __init__(self, control: ParticipantControl) -> None:
        self._control = control

    def register(
        self,
        manifest: ParticipantManifest,
        protected_endpoint: socket.socket,
    ) -> ParticipantRegistration:
        """Register only the manifest composed with this control."""
        del manifest
        return self._control.register(protected_endpoint)

    def notices(self) -> Iterator[ParticipantNotice]:
        """Yield exact decoded Claude notices."""
        return self._control.notices()

    def begin(self, turn_id: TurnId) -> TurnAdmission:
        """Return one exact turn boundary."""
        return self._control.begin(turn_id)

    def end(self, turn_id: TurnId) -> None:
        """Close one exact turn boundary."""
        self._control.end(turn_id)

    def ready(self, proof: ParticipantReadyProof) -> None:
        """Publish one exact readiness proof."""
        self._control.ready(proof)

    def adopted(self, proof: ParticipantAdoptionProof) -> None:
        """Publish one exact adoption proof."""
        self._control.adopted(proof)

    def close(self) -> None:
        """Close both supervisor clients."""
        self._control.close()


class ClaudeControl(Protocol):
    """Structural participant operations accepted by one attachment."""

    def register(
        self,
        manifest: ParticipantManifest,
        protected_endpoint: socket.socket,
    ) -> ParticipantRegistration: ...

    def notices(self) -> Iterator[ParticipantNotice]: ...

    def begin(self, turn_id: TurnId) -> TurnAdmission: ...

    def end(self, turn_id: TurnId) -> None: ...

    def ready(self, proof: ParticipantReadyProof) -> None: ...

    def adopted(self, proof: ParticipantAdoptionProof) -> None: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class ClaudeCoordination:
    """One replaceable supervisor attachment and protected endpoint."""

    control: ClaudeControl
    host_endpoint: socket.socket
    registration_endpoint: socket.socket

    def register(
        self,
        manifest: ParticipantManifest,
        binding: ClaudeStructuredBinding | None,
    ) -> tuple[ClaudeProtectedHostChannel, ParticipantRegistration]:
        """Register while exactly one temporary reader reports binding."""
        channel = ClaudeProtectedHostChannel(
            self.host_endpoint,
            manifest.participant_id,
            manifest.connection_generation,
        )
        result: Queue[BaseException | None] = Queue(maxsize=1)
        reporter = Thread(
            target=_report_binding,
            args=(channel, binding, result),
            daemon=True,
            name="claude-binding-report",
        )
        reporter.start()
        try:
            registration = self.control.register(
                manifest,
                self.registration_endpoint,
            )
        except BaseException:
            channel.close()
            reporter.join(timeout=_REPORTER_JOIN_SECONDS)
            raise
        reporter.join(timeout=_REPORTER_JOIN_SECONDS)
        if reporter.is_alive():
            channel.close()
            raise RuntimeError("Claude binding report did not complete.")
        error = result.get_nowait()
        if error is not None:
            channel.close()
            raise error
        return channel, registration


class ClaudeCoordinationFactory(Protocol):
    """Create one exact replaceable supervisor attachment."""

    def __call__(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
    ) -> ClaudeCoordination:
        """Return a fresh attachment for one participant generation."""


class ClaudeSupervisorCoordinationFactory:
    """Create exact owner-only attachments to one supervisor endpoint."""

    def __init__(self, supervisor_socket: Path) -> None:
        self._supervisor_socket = supervisor_socket

    def __call__(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
    ) -> ClaudeCoordination:
        manifest = ParticipantManifest(
            participant_id=participant_id,
            provider_id=ProviderId.CLAUDE,
            client_kind=ParticipantClientKind.CLAUDE_CODE,
            capability_version=1,
            connection_generation=connection_generation,
        )
        action = ControlClient.connect(self._supervisor_socket)
        subscription: ControlClient | None = None
        host: socket.socket | None = None
        registration: socket.socket | None = None
        try:
            subscription = ControlClient.connect(self._supervisor_socket)
            host, registration = socket.socketpair(
                socket.AF_UNIX, socket.SOCK_STREAM
            )
            control = ClaudeParticipantControl(
                ParticipantControl(action, subscription, manifest)
            )
            return ClaudeCoordination(control, host, registration)
        except BaseException:
            for resource in (registration, host, subscription, action):
                if resource is not None:
                    resource.close()
            raise


def _report_binding(
    channel: ClaudeProtectedHostChannel,
    binding: ClaudeStructuredBinding | None,
    result: Queue[BaseException | None],
) -> None:
    try:
        channel.report_current_binding(binding)
    except BaseException as error:
        result.put(error)
    else:
        result.put(None)

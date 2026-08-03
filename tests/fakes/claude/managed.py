"""Synthetic managed Claude profiles and process behavior."""

import json
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

import pytest

import sidekick_usages.platform.executable
from sidekick_usages.core.accounts.types import (
    OperationId,
    ProviderIdentity,
    RequestId,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import SelectionEpoch
from sidekick_usages.core.selection.types import TurnId
from sidekick_usages.paths import (
    ApplicationPaths,
    managed_claude_config_dir,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.platform.models import ExecutableProvenance
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.auth.generation import (
    claude_access_token_generation,
)
from sidekick_usages.providers.claude.auth.login.models import ClaudeAuthStatus
from sidekick_usages.providers.claude.auth.login.service import (
    claude_status_association_key,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from sidekick_usages.providers.claude.environment import (
    claude_structured_environment,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.executable import (
    MINIMUM_CLAUDE_VERSION,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.managed.types import (
    ClaudeManagedPlatform,
)
from sidekick_usages.providers.claude.models import (
    ClaudeCommandResult,
    ClaudeExecutable,
    ClaudeManagedProfile,
    ClaudeNativeProfile,
    ClaudeVersion,
)
from sidekick_usages.providers.claude.structured.codec import (
    clear_secret_buffer,
)
from sidekick_usages.providers.claude.structured.data_plane import (
    ClaudeProtectedOAuthFrame,
)
from sidekick_usages.providers.claude.structured.models import (
    ClaudeStructuredAdoptionReceipt,
    ClaudeStructuredBinding,
    ClaudeStructuredError,
    ClaudeStructuredFailure,
    ClaudeStructuredInstallReceipt,
)
from sidekick_usages.providers.claude.structured.process import (
    CLAUDE_STRUCTURED_ARTIFACT_SHA256,
    CLAUDE_STRUCTURED_ARTIFACT_SIZE,
    CLAUDE_STRUCTURED_PROBE_CANARY,
    ClaudeStructuredProcess,
)
from sidekick_usages.providers.claude.structured.session import (
    ClaudeStructuredSession,
)
from sidekick_usages.providers.claude.types import (
    ClaudeProcessFailure,
    ClaudeProfile,
)
from sidekick_usages.serialization.json import (
    JsonObject,
    decode_json_object,
    encode_compact_json,
)

type ClaudeCommandScript = Callable[
    [
        tuple[str, ...],
        dict[str, str] | None,
        Path | None,
    ],
    ClaudeCommandResult,
]
type ClaudeProfileResponses = Mapping[
    tuple[Path, tuple[str, ...]],
    ClaudeCommandResult,
]

CLAUDE_LOGIN_HELP_OUTPUT = b"Usage: claude auth login [--claudeai]\n"
CLAUDE_LOGGED_IN_STATUS = (
    b'{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty",'
    b'"email":"external@example.test","orgId":'
    b'"provider-organization-external",'
    b'"orgName":"External Organization","subscriptionType":"team"}\n'
)
CLAUDE_LOGGED_OUT_STATUS = (
    b'{"loggedIn":false,"authMethod":"none","apiProvider":"firstParty"}\n'
)
CLAUDE_VERSION_OUTPUT = b"2.1.220 (Claude Code)\n"
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
_NANOSECONDS_PER_MILLISECOND = 1_000_000
_STRUCTURED_FAILURES = {
    "timeout": ClaudeStructuredFailure.PROTOCOL_TIMEOUT,
    "eof": ClaudeStructuredFailure.PROTOCOL_EOF,
    "process_error": ClaudeStructuredFailure.PROCESS_EXITED,
}


class StructuredResponseCase(StrEnum):
    """Synthetic structured-engine response behaviors."""

    SUCCESS = "success"
    WRONG_REQUEST = "wrong_request"
    REPLAY = "replay"
    OVERSIZE = "oversize"
    MALFORMED_UTF8 = "malformed_utf8"
    MULTIPLE_JSON = "multiple_json"
    DUPLICATE_RESPONSE = "duplicate_response"
    TIMEOUT = "timeout"
    EOF = "eof"
    PROCESS_ERROR = "process_error"
    ERROR_RESPONSE = "error_response"
    ERROR_OLD_TEXT = "error_old_text"
    ERROR_UNRELATED = "error_unrelated"
    ERROR_EXTRA_FIELDS = "error_extra_fields"
    EXTRA_FIELDS = "extra_fields"


@dataclass(frozen=True, slots=True)
class StructuredRequestObservation:
    """Secret-free observation of one synthetic control request."""

    request_id: str
    variable_names: tuple[str, ...]
    exact_envelope: bool
    expected_oauth: bool


class ClaudeStructuredEngineFake:
    """Record secret-free structured behavior for one stable fake PID."""

    def __init__(
        self,
        responses: tuple[StructuredResponseCase, ...],
        expected_oauth_values: tuple[str | int, ...],
        *,
        process_id: int = 4242,
    ) -> None:
        self._responses = list(responses)
        self._expected_oauth_values = list(expected_oauth_values)
        self._first_request_id: str | None = None
        self.process_id = process_id
        self.requests: list[StructuredRequestObservation] = []
        self.cleared_request_buffers: list[bytearray] = []
        self.wiped_before_response: list[bool] = []
        self.events: list[tuple[str, str]] = []
        self.user_turn_count = 0
        self.input_closed = False

    def exchange(
        self,
        request: bytearray,
        request_id: RequestId,
        timeout_seconds: float,
    ) -> bytes:
        """Return one scripted response without retaining OAuth bytes."""
        del timeout_seconds
        root = decode_json_object(request[:-1])
        encoded_request_id = root.get("request_id")
        if not isinstance(encoded_request_id, str):
            raise AssertionError("Structured request ID is invalid.")
        if encoded_request_id != str(request_id):
            raise AssertionError("Structured request correlation changed.")
        variables = root.get("variables")
        if not isinstance(variables, dict):
            raise AssertionError("Structured variables are invalid.")
        oauth = variables.get("CLAUDE_CODE_OAUTH_TOKEN")
        expected_oauth = self._expected_oauth_values.pop(0)
        self.requests.append(
            StructuredRequestObservation(
                request_id=encoded_request_id,
                variable_names=tuple(sorted(variables)),
                exact_envelope=(
                    set(root) == {"request_id", "type", "variables"}
                    and root.get("type") == "update_environment_variables"
                    and request[-1:] == b"\n"
                    and request[:-1] == encode_compact_json(root)
                ),
                expected_oauth=oauth == expected_oauth,
            )
        )
        self.cleared_request_buffers.append(request)
        if self._first_request_id is None:
            self._first_request_id = encoded_request_id
        response_case = self._responses.pop(0)
        clear_secret_buffer(request)
        self.wiped_before_response.append(not any(request))
        return self._response(response_case, encoded_request_id)

    def transmit_turn(self, receipt: ClaudeStructuredAdoptionReceipt) -> None:
        """Record that adoption existed before one real prompt."""
        epoch = str(receipt.binding.epoch.value)
        self.events.extend((("adoption", epoch), ("prompt", epoch)))
        self.user_turn_count += 1

    def send_interactive(
        self, frame: bytearray, timeout_seconds: float
    ) -> None:
        """Record one bounded fake interactive input frame."""
        clear_secret_buffer(frame)

    def receive_event(self, timeout_seconds: float) -> bytes:
        """Return one queued fake interactive event frame."""
        return b"{}"

    def close_input(self) -> None:
        """Close the synthetic probe input without a signal."""
        self.input_closed = True

    def wait(self, timeout_seconds: float) -> int:
        """Return one ordinary synthetic child exit status."""
        del timeout_seconds
        return 0

    def __repr__(self) -> str:
        """Hide every scripted request detail."""
        return "<ClaudeStructuredEngineFake redacted>"

    def _response(
        self,
        response_case: StructuredResponseCase,
        request_id: str,
    ) -> bytes:
        failure = _STRUCTURED_FAILURES.get(response_case.value)
        if failure is not None:
            raise ClaudeStructuredError(failure)
        fixed = {
            StructuredResponseCase.OVERSIZE: b"x" * 65_537,
            StructuredResponseCase.MALFORMED_UTF8: b"\xff",
            StructuredResponseCase.MULTIPLE_JSON: b"{}{}",
        }.get(response_case)
        if fixed is not None:
            return fixed
        response_id = request_id
        if response_case is StructuredResponseCase.WRONG_REQUEST:
            response_id = "99999999-9999-4999-8999-999999999999"
        if response_case is StructuredResponseCase.REPLAY:
            if self._first_request_id is None:
                raise AssertionError("Replay requires one prior request.")
            response_id = self._first_request_id
        structured_error = (
            "update_environment_variables: variables must be an object of "
            "string values"
        )
        old_error = "Environment variable values must be strings."
        unrelated_error = "unrelated bounded failure"
        error_text = {
            StructuredResponseCase.ERROR_RESPONSE: structured_error,
            StructuredResponseCase.ERROR_OLD_TEXT: old_error,
            StructuredResponseCase.ERROR_UNRELATED: unrelated_error,
            StructuredResponseCase.ERROR_EXTRA_FIELDS: structured_error,
        }.get(response_case)
        is_error = error_text is not None
        subtype = "error" if is_error else "success"
        response: JsonObject = {"request_id": response_id, "subtype": subtype}
        if is_error:
            response["error"] = error_text
        if response_case in {
            StructuredResponseCase.ERROR_EXTRA_FIELDS,
            StructuredResponseCase.EXTRA_FIELDS,
        }:
            response["unexpected"] = True
        encoded = encode_compact_json(
            {"response": response, "type": "control_response"}
        )
        if response_case is StructuredResponseCase.DUPLICATE_RESPONSE:
            return _duplicate_response(encoded, RequestId(request_id))
        return encoded


def _duplicate_response(encoded: bytes, request_id: RequestId) -> bytes:
    event = b'{"type":"synthetic_event"}'
    transport = ClaudeStructuredProcess.__new__(ClaudeStructuredProcess)
    transport._buffer = bytearray(event + b"\n" + encoded + b"\n")
    transport._event_frames = []
    transport._event_bytes = 0
    transport._monotonic = lambda: 0.0
    assert transport._receive(request_id, 1.0) == encoded
    assert transport.take_events() == (event,)

    transport._buffer = bytearray(encoded + b"\n" + encoded + b"\n")
    with pytest.raises(ClaudeStructuredError) as duplicate:
        transport._receive(request_id, 1.0)
    assert duplicate.value.code is ClaudeStructuredFailure.PROTOCOL_MALFORMED
    return encoded + b"\n" + encoded


class ClaudeStructuredEngineFactoryFake:
    """Open isolated structured probe children without provider access."""

    def __init__(
        self,
        positive_canary: str,
        positive_response: StructuredResponseCase = (
            StructuredResponseCase.SUCCESS
        ),
        negative_response: StructuredResponseCase = (
            StructuredResponseCase.ERROR_RESPONSE
        ),
    ) -> None:
        self._positive_canary = positive_canary
        self._positive_response = positive_response
        self._negative_response = negative_response
        self.engines: list[ClaudeStructuredEngineFake] = []
        self.environments: list[dict[str, str]] = []

    def __call__(
        self,
        executable: ClaudeExecutable,
        environment: Mapping[str, str],
        *,
        working_directory: Path,
        user_arguments: tuple[str, ...] = (),
    ) -> ClaudeStructuredEngineFake:
        """Return one two-exchange no-network probe engine."""
        del executable
        if user_arguments:
            raise AssertionError("Capability probe received user arguments.")
        engine = ClaudeStructuredEngineFake(
            (
                self._positive_response,
                self._negative_response,
            ),
            (self._positive_canary, 7),
            process_id=5252,
        )
        self.engines.append(engine)
        self.environments.append(dict(environment))
        del working_directory
        return engine


class StructuredCapabilityMutation(StrEnum):
    """Independent exact-capability mismatches for one journey table."""

    VERSION = "version"
    HASH = "hash"
    IDENTITY = "identity"
    SIZE = "size"
    MANIFEST = "manifest"
    SCHEMA = "schema"
    NEGATIVE_SCHEMA = "negative_schema"
    NEGATIVE_OLD_ERROR = "negative_old_error"
    NEGATIVE_UNRELATED_ERROR = "negative_unrelated_error"
    MACOS = "macos"


@dataclass(frozen=True, slots=True)
class StructuredCapabilityFixture:
    """Controlled artifact, process, and environment qualification inputs."""

    executable: ClaudeExecutable
    host: HostPlatform
    environment: dict[str, str]
    working_directory: Path
    factory: ClaudeStructuredEngineFactoryFake
    mutation: StructuredCapabilityMutation | None

    def inspect_artifact(
        self,
        candidate: ClaudeExecutable,
        markers: tuple[bytes, ...],
    ) -> tuple[str, frozenset[bytes]]:
        """Return exact or independently mutated artifact evidence."""
        if candidate is not self.executable:
            raise AssertionError("Capability inspected another executable.")
        digest = (
            "0" * 64
            if self.mutation is StructuredCapabilityMutation.HASH
            else CLAUDE_STRUCTURED_ARTIFACT_SHA256
        )
        observed = (
            frozenset()
            if self.mutation is StructuredCapabilityMutation.MANIFEST
            else frozenset(markers)
        )
        return digest, observed


def structured_capability_fixture(
    root: Path,
    mutation: StructuredCapabilityMutation | None,
) -> StructuredCapabilityFixture:
    """Create one exact or independently mismatched capability boundary."""
    version = (
        ClaudeVersion(2, 1, 221)
        if mutation is StructuredCapabilityMutation.VERSION
        else MINIMUM_CLAUDE_VERSION
    )
    size = (
        1
        if mutation is StructuredCapabilityMutation.SIZE
        else CLAUDE_STRUCTURED_ARTIFACT_SIZE
    )
    artifact = root / "versions" / str(version) / "claude"
    artifact.parent.mkdir(parents=True)
    with artifact.open("wb") as stream:
        stream.truncate(size)
    artifact.chmod(0o755)
    launcher = root / "bin" / "claude"
    launcher.parent.mkdir()
    launcher.symlink_to(artifact)
    executable = ClaudeExecutable(
        launcher,
        ExecutableProvenance.from_stat(artifact, artifact.stat()),
        version,
    )
    if mutation is StructuredCapabilityMutation.IDENTITY:
        replacement = artifact.with_name("claude-replacement")
        with replacement.open("wb") as stream:
            stream.truncate(CLAUDE_STRUCTURED_ARTIFACT_SIZE)
        replacement.chmod(0o755)
        launcher.unlink()
        launcher.symlink_to(replacement)
    profile = native_profile(root / "home")
    negative_responses: dict[
        StructuredCapabilityMutation | None,
        StructuredResponseCase,
    ] = {
        StructuredCapabilityMutation.NEGATIVE_SCHEMA: (
            StructuredResponseCase.ERROR_EXTRA_FIELDS
        ),
        StructuredCapabilityMutation.NEGATIVE_OLD_ERROR: (
            StructuredResponseCase.ERROR_OLD_TEXT
        ),
        StructuredCapabilityMutation.NEGATIVE_UNRELATED_ERROR: (
            StructuredResponseCase.ERROR_UNRELATED
        ),
    }
    negative_response = negative_responses.get(
        mutation,
        StructuredResponseCase.ERROR_RESPONSE,
    )
    factory = ClaudeStructuredEngineFactoryFake(
        CLAUDE_STRUCTURED_PROBE_CANARY,
        (
            StructuredResponseCase.EXTRA_FIELDS
            if mutation is StructuredCapabilityMutation.SCHEMA
            else StructuredResponseCase.SUCCESS
        ),
        negative_response,
    )
    return StructuredCapabilityFixture(
        executable=executable,
        host=(
            HostPlatform.MACOS_ARM64
            if mutation is StructuredCapabilityMutation.MACOS
            else HostPlatform.LINUX
        ),
        environment=claude_structured_environment(
            {"PATH": "/synthetic/bin"},
            profile,
        ),
        working_directory=profile.config_directory,
        factory=factory,
        mutation=mutation,
    )


@dataclass(frozen=True, slots=True)
class StructuredSessionFixture:
    """One session with two exact synthetic authority transitions."""

    engine: ClaudeStructuredEngineFake
    session: ClaudeStructuredSession
    initial_frame: ClaudeProtectedOAuthFrame
    initial_install: ClaudeStructuredInstallReceipt
    binding_a: ClaudeStructuredBinding
    binding_b: ClaudeStructuredBinding
    binding_c: ClaudeStructuredBinding
    oauth_a: str
    oauth_b: str
    oauth_c: str
    turn_id: TurnId
    request_id_a: RequestId
    request_id_b: RequestId
    request_id_c: RequestId


def structured_session_fixture(
    response: StructuredResponseCase,
) -> StructuredSessionFixture:
    """Create one stable-PID session for the full protocol journey."""

    def binding(
        operation: str,
        account: str,
        oauth: str,
        epoch: int,
    ) -> ClaudeStructuredBinding:
        return ClaudeStructuredBinding(
            operation_id=OperationId(operation),
            account_id=SidekickAccountId(account),
            generation=claude_access_token_generation(oauth),
            epoch=SelectionEpoch(epoch),
        )

    oauth_a = "synthetic-structured-oauth-a"
    oauth_b = "synthetic-structured-oauth-b"
    oauth_c = "synthetic-structured-oauth-c"
    binding_a = binding(
        "00000000-0000-4000-8000-000000000000",
        "11111111-1111-4111-8111-111111111111",
        oauth_a,
        7,
    )
    binding_b = binding(
        "55555555-5555-4555-8555-555555555555",
        "22222222-2222-4222-8222-222222222222",
        oauth_b,
        8,
    )
    binding_c = binding(
        "66666666-6666-4666-8666-666666666666",
        "44444444-4444-4444-8444-444444444444",
        oauth_c,
        9,
    )
    request_id_a = RequestId("10101010-1010-4010-8010-101010101010")
    request_id_b = RequestId("77777777-7777-4777-8777-777777777777")
    request_id_c = RequestId("88888888-8888-4888-8888-888888888888")
    engine = ClaudeStructuredEngineFake(
        (
            StructuredResponseCase.SUCCESS,
            StructuredResponseCase.SUCCESS,
            response,
        ),
        (oauth_a, oauth_b, oauth_c),
    )
    request_ids = iter((request_id_a, request_id_b, request_id_c))
    initial_frame = ClaudeProtectedOAuthFrame(
        binding_a,
        bytearray(oauth_a, encoding="utf-8"),
    )
    session, initial_install = ClaudeStructuredSession.bootstrap(
        engine,
        initial_frame,
        request_id_factory=lambda: next(request_ids),
    )
    return StructuredSessionFixture(
        engine=engine,
        session=session,
        initial_frame=initial_frame,
        initial_install=initial_install,
        binding_a=binding_a,
        binding_b=binding_b,
        binding_c=binding_c,
        oauth_a=oauth_a,
        oauth_b=oauth_b,
        oauth_c=oauth_c,
        turn_id=TurnId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        request_id_a=request_id_a,
        request_id_b=request_id_b,
        request_id_c=request_id_c,
    )


def use_synthetic_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the Python executable as the synthetic Claude CLI."""
    monkeypatch.setattr(
        sidekick_usages.platform.executable.shutil,
        "which",
        lambda command, path=None: (
            sys.executable if command == "claude" else None
        ),
    )


class ClaudeRunner:
    """Record exact Claude commands and return one synthetic script."""

    def __init__(
        self,
        responses: Mapping[tuple[str, ...], ClaudeCommandResult] | None = None,
        *,
        profile_responses: ClaudeProfileResponses | None = None,
        script: ClaudeCommandScript | None = None,
    ) -> None:
        if (responses is None) == (script is None):
            raise ValueError("Claude runner requires one response source.")
        self._responses = responses
        self._profile_responses = dict(profile_responses or {})
        self._script = script
        self.calls: list[tuple[Path, tuple[str, ...]]] = []
        self.environments: list[dict[str, str] | None] = []
        self.working_directories: list[Path | None] = []
        self.timeouts: list[float] = []
        self.output_limits: list[int] = []
        self.umasks: list[int] = []

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        maximum_output_bytes: int,
        environment: Mapping[str, str] | None = None,
        working_directory: Path | None = None,
        umask: int = -1,
        cancelled: Callable[[], bool] | None = None,
    ) -> ClaudeCommandResult:
        if cancelled is not None and cancelled():
            raise ClaudeProcessError(ClaudeProcessFailure.CANCELLED)
        arguments = argv[1:]
        captured_environment = (
            None if environment is None else dict(environment)
        )
        self.calls.append((Path(argv[0]), arguments))
        self.environments.append(captured_environment)
        self.working_directories.append(working_directory)
        self.timeouts.append(timeout_seconds)
        self.output_limits.append(maximum_output_bytes)
        self.umasks.append(umask)
        if self._script is not None:
            return self._script(
                arguments,
                captured_environment,
                working_directory,
            )
        profile = self._profile_directory(captured_environment)
        if profile is not None:
            response = self._profile_responses.get((profile, arguments))
            if response is not None:
                return response
        if self._responses is None:
            raise AssertionError("Claude responses are unavailable.")
        try:
            return self._responses[arguments]
        except KeyError:
            raise AssertionError(
                f"Unexpected Claude command: {arguments!r}"
            ) from None

    @staticmethod
    def _profile_directory(
        environment: Mapping[str, str] | None,
    ) -> Path | None:
        if environment is None:
            return None
        configured = environment.get("CLAUDE_CONFIG_DIR")
        if configured is not None:
            return Path(configured)
        home = environment.get("HOME")
        return None if home is None else Path(home) / ".claude"


class ClaudeManagedLoginScript:
    """Write scripted official-login results into exact private profiles."""

    def __init__(
        self,
        profiles: PrivateCredentialTree,
        refresh_payloads: Mapping[Path, tuple[bytes | None, ...]],
        *,
        interactive_payloads: Mapping[Path, bytes] | None = None,
        interactive_statuses: Mapping[Path, tuple[bytes, ...]] | None = None,
        profile_statuses: Mapping[Path, bytes] | None = None,
        refresh_statuses: Mapping[Path, tuple[bytes, ...]] | None = None,
        advance_native_mtime: bool = True,
    ) -> None:
        self._profiles = profiles
        self._refresh_payloads = {
            profile: list(payloads)
            for profile, payloads in refresh_payloads.items()
        }
        self._interactive_payloads = dict(interactive_payloads or {})
        self._interactive_statuses = {
            profile: list(statuses)
            for profile, statuses in (interactive_statuses or {}).items()
        }
        self._profile_statuses = dict(profile_statuses or {})
        self._refresh_statuses = {
            profile: list(statuses)
            for profile, statuses in (refresh_statuses or {}).items()
        }
        self._advance_native_mtime = advance_native_mtime
        self.login_profiles: list[Path] = []
        self.interactive_profiles: list[Path] = []

    def __call__(
        self,
        arguments: tuple[str, ...],
        environment: dict[str, str] | None,
        working_directory: Path | None,
    ) -> ClaudeCommandResult:
        del working_directory
        if arguments == ("--version",):
            return ClaudeCommandResult(0, CLAUDE_VERSION_OUTPUT)
        if arguments == ("auth", "login", "--help"):
            return ClaudeCommandResult(0, CLAUDE_LOGIN_HELP_OUTPUT)
        if arguments == ("auth", "status"):
            config_directory = self._config_directory(environment)
            configured = self._profile_statuses.get(config_directory)
            if configured is not None:
                return ClaudeCommandResult(0, configured)
            credential_file = config_directory / CLAUDE_CREDENTIAL_FILE
            return (
                ClaudeCommandResult(0, CLAUDE_LOGGED_IN_STATUS)
                if credential_file.is_file()
                else ClaudeCommandResult(1, CLAUDE_LOGGED_OUT_STATUS)
            )
        if arguments != ("auth", "login", "--claudeai"):
            raise AssertionError(f"Unexpected Claude command: {arguments!r}")
        config_directory = self._config_directory(environment)
        try:
            payload = self._refresh_payloads[config_directory].pop(0)
        except KeyError, IndexError:
            raise AssertionError(
                "Official login targeted an unexpected profile."
            ) from None
        self.login_profiles.append(config_directory)
        if payload is None:
            return ClaudeCommandResult(1, b"synthetic login rejected")
        self._write_credentials(config_directory, payload)
        statuses = self._refresh_statuses.get(config_directory)
        if statuses:
            self._profile_statuses[config_directory] = statuses.pop(0)
        return ClaudeCommandResult(0, b"synthetic official login complete")

    def interactive(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        environment: Mapping[str, str] | None = None,
        working_directory: Path | None = None,
        umask: int = -1,
    ) -> int:
        """Write one provider-controlled interactive login result."""
        del timeout_seconds, working_directory, umask
        if argv[1:] != ("auth", "login", "--claudeai"):
            raise AssertionError(
                f"Unexpected interactive Claude command: {argv[1:]!r}"
            )
        config_directory = self._config_directory(environment)
        try:
            payload = self._interactive_payloads[config_directory]
        except KeyError:
            raise AssertionError(
                "Interactive login targeted an unexpected profile."
            ) from None
        self._write_credentials(config_directory, payload)
        statuses = self._interactive_statuses.get(config_directory)
        if statuses:
            self._profile_statuses[config_directory] = statuses.pop(0)
        self.interactive_profiles.append(config_directory)
        return 0

    def set_status(
        self,
        config_directory: Path,
        payload: bytes,
    ) -> None:
        """Set explicit provider profile state after an external login."""
        self._profile_statuses[config_directory] = payload

    def set_authority(
        self,
        config_directory: Path,
        credentials: bytes,
        status: bytes,
    ) -> None:
        """Apply one complete external provider authority transition."""
        self._write_credentials(config_directory, credentials)
        self._profile_statuses[config_directory] = status

    def _write_credentials(
        self,
        config_directory: Path,
        payload: bytes,
    ) -> None:
        if config_directory.is_relative_to(self._profiles.root):
            self._profiles.write_owned_file(
                config_directory,
                CLAUDE_CREDENTIAL_FILE,
                payload,
            )
            return
        credential_file = config_directory / CLAUDE_CREDENTIAL_FILE
        previous_modified = (
            credential_file.stat().st_mtime_ns
            if credential_file.is_file()
            else None
        )
        credential_file.write_bytes(payload)
        os.chmod(credential_file, _PRIVATE_FILE_MODE)
        if previous_modified is not None:
            current = credential_file.stat()
            modified = (
                previous_modified + _NANOSECONDS_PER_MILLISECOND
                if self._advance_native_mtime
                else previous_modified
            )
            os.utime(
                credential_file,
                ns=(current.st_atime_ns, modified),
            )

    @staticmethod
    def _config_directory(
        environment: Mapping[str, str] | None,
    ) -> Path:
        if environment is None:
            raise AssertionError("Claude process environment was omitted.")
        configured = environment.get("CLAUDE_CONFIG_DIR")
        if configured is not None:
            return Path(configured)
        return Path(environment["HOME"]) / ".claude"


def credential_payload(
    account_id: str | None,
    organization_id: str | None,
    *,
    token_suffix: str,
    access_expires_at: datetime,
    refresh_expires_at: datetime | None = None,
    scopes: tuple[str, ...] = ("user:profile", "user:inference"),
) -> bytes:
    """Encode one complete synthetic Claude credential envelope."""
    if (account_id is None) != (organization_id is None):
        raise ValueError("Synthetic Claude identity must be complete.")
    oauth: dict[str, object] = {
        "accessToken": f"sk-ant-oat01-{token_suffix}",
        "refreshToken": f"refresh-{token_suffix}",
        "expiresAt": int(access_expires_at.timestamp() * 1000),
        "subscriptionType": "pro",
        "scopes": list(scopes),
    }
    if account_id is not None and organization_id is not None:
        oauth["tokenAccount"] = {
            "accountUuid": account_id,
            "organizationUuid": organization_id,
        }
    if refresh_expires_at is not None:
        oauth["refreshTokenExpiresAt"] = int(
            refresh_expires_at.timestamp() * 1000
        )
    return json.dumps(
        {
            "claudeAiOauth": oauth,
        }
    ).encode()


def claude_auth_status_payload(
    email: str,
    organization_id: str,
    *,
    subscription_type: str = "pro",
) -> bytes:
    """Encode one exact synthetic official Claude status."""
    return json.dumps(
        {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "email": email,
            "orgId": organization_id,
            "orgName": "Synthetic Organization",
            "subscriptionType": subscription_type,
        }
    ).encode()


def claude_auth_status_result(
    email: str,
    organization_id: str,
) -> ClaudeCommandResult:
    """Return one successful exact-profile official status result."""
    return ClaudeCommandResult(
        0,
        claude_auth_status_payload(email, organization_id),
    )


def claude_profile_status_responses(
    profiles: Mapping[Path, str],
) -> dict[tuple[Path, tuple[str, ...]], ClaudeCommandResult]:
    """Return explicit auth-status responses for exact profiles."""
    return {
        (profile, ("auth", "status")): claude_auth_status_result(
            f"{name}@example.test",
            f"provider-organization-{name}",
        )
        for profile, name in profiles.items()
    }


def claude_profile_status(
    name: str,
) -> tuple[bytes, ProviderIdentity]:
    """Return explicit status and association for one named profile."""
    email = f"{name}@example.test"
    organization_id = f"provider-organization-{name}"
    return (
        claude_auth_status_payload(email, organization_id),
        claude_status_identity(email, organization_id),
    )


def claude_status_identity(
    email: str,
    organization_id: str,
) -> ProviderIdentity:
    """Return the production association key for synthetic status."""
    identity = claude_status_association_key(
        ClaudeAuthStatus(
            return_code=0,
            logged_in=True,
            auth_method="claude.ai",
            api_provider="firstParty",
            email=email,
            organization_id=organization_id,
        )
    )
    if identity is None:
        raise AssertionError("Synthetic Claude status must be complete.")
    return identity


def claude_capabilities(
    profile: ClaudeProfile,
    platform: ClaudeManagedPlatform,
) -> ClaudeCapabilities:
    """Return capability-matched evidence for one synthetic profile."""
    executable_path = Path(sys.executable).resolve()
    return ClaudeCapabilities(
        ClaudeExecutable(
            executable_path,
            ExecutableProvenance.from_stat(
                executable_path,
                executable_path.stat(),
            ),
            MINIMUM_CLAUDE_VERSION,
        ),
        profile,
        platform,
    )


def managed_profile(
    paths: ApplicationPaths,
    account_id: SidekickAccountId,
) -> ClaudeManagedProfile:
    """Return the exact synthetic profile for one stable account."""
    return ClaudeManagedProfile(
        account_id,
        managed_claude_config_dir(paths, account_id),
    )


def native_profile(root: Path) -> ClaudeNativeProfile:
    """Create one secure synthetic native-default Claude profile."""
    root.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    profile = ClaudeNativeProfile(root / ".claude")
    profile.config_directory.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    return profile


def profile_tree(paths: ApplicationPaths) -> PrivateCredentialTree:
    """Return the exact managed Claude private tree."""
    return PrivateCredentialTree(
        paths.private_claude_profiles,
        account_path=paths.accounts,
    )

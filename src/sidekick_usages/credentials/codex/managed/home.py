"""Qualified managed Codex private-home access."""

from collections.abc import Mapping

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.models import CodexCredentials
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.codex.models import CodexProjectionLease
from sidekick_usages.paths import ApplicationPaths, managed_codex_home
from sidekick_usages.persistence.errors import PersistenceFilesystemError
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.types.error import PersistenceCode
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.app_server.models import (
    CodexAppServerCapabilities,
)
from sidekick_usages.providers.codex.app_server.session import (
    CodexAppServerSession,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexProcessGroupPolicy,
)
from sidekick_usages.providers.codex.auth import (
    CODEX_AUTH_FILE,
    CODEX_CONFIG_FILE,
    managed_auth_snapshot,
    parse_managed_auth_credentials,
    parse_managed_auth_snapshot,
    prepare_file_auth_config,
)
from sidekick_usages.providers.codex.models import (
    CodexAuthSnapshot,
)

_PERSISTENCE_FAILURE_KINDS = {
    PersistenceCode.UNSUPPORTED_FILESYSTEM: ProviderFailureKind.UNSUPPORTED,
    PersistenceCode.UNSAFE_PERMISSIONS: ProviderFailureKind.MALFORMED,
    PersistenceCode.UNREADABLE: ProviderFailureKind.UNREADABLE,
    PersistenceCode.INVALID_SCHEMA: ProviderFailureKind.MALFORMED,
}


class CodexPrivateHomeAuthority:
    """Qualified private-home access shared by Codex workflows."""

    def __init__(
        self,
        paths: ApplicationPaths,
        private: PrivateCredentialTree,
        capabilities: CodexAppServerCapabilities,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if private.root != paths.private_codex_profiles:
            raise ValueError("Managed Codex tree does not match app paths.")
        self._paths = paths
        self._private = private
        self._capabilities = capabilities
        self._environment = None if environment is None else dict(environment)

    @property
    def executable_version(self) -> str:
        """Return the exact capability-proven Codex version."""
        return str(self._capabilities.executable.version)

    def configure(
        self,
        account_id: SidekickAccountId,
    ) -> ProviderFailure | None:
        """Create or verify one final home with file-backed auth storage."""
        relative = str(account_id)
        home = managed_codex_home(self._paths, account_id)
        try:
            present = self._private.relative_bundle_present(relative)
            current = self._private.read_relative_bundle_file(
                relative,
                CODEX_CONFIG_FILE,
            )
            prepared = prepare_file_auth_config(
                None if current is None else current.data
            )
            if isinstance(prepared, ProviderFailure):
                return prepared
            if current is not None and current.data == prepared:
                return None
            self._private.write_bundle(
                home,
                {CODEX_CONFIG_FILE: prepared},
                expected_bundle_present=present,
                expected_files={
                    CODEX_CONFIG_FILE: (
                        None if current is None else current.data
                    )
                },
            )
        except PersistenceFilesystemError as error:
            return _private_failure(
                error,
                "The managed Codex home cannot be prepared safely.",
            )
        return None

    def snapshot(
        self,
        account_id: SidekickAccountId,
    ) -> CodexAuthSnapshot | ProviderFailure:
        """Read protected identity and generation through qualified paths."""
        files = self._read_authority(account_id)
        if isinstance(files, ProviderFailure):
            return files
        auth_payload, config_payload = files
        return parse_managed_auth_snapshot(auth_payload, config_payload)

    def projection(
        self,
        account_id: SidekickAccountId,
        expected: CodexAuthSnapshot,
    ) -> CodexProjectionLease | ProviderFailure:
        """Open one locally identity-bound access-token projection."""
        files = self._read_authority(account_id)
        if isinstance(files, ProviderFailure):
            return files
        auth_payload, config_payload = files
        detected = parse_managed_auth_credentials(
            auth_payload,
            config_payload,
        )
        if isinstance(detected, ProviderFailure):
            return detected
        snapshot = managed_auth_snapshot(detected)
        if isinstance(snapshot, ProviderFailure):
            return snapshot
        credentials = detected.credentials
        if (
            not isinstance(credentials, CodexCredentials)
            or credentials.account_id is None
            or snapshot.provider_identity != expected.provider_identity
            or snapshot.generation != expected.generation
            or credentials.account_id != str(expected.provider_identity)
        ):
            return ProviderFailure(
                provider_id=ProviderId.CODEX,
                kind=ProviderFailureKind.IDENTITY_MISMATCH,
                message=(
                    "The managed Codex projection identity is inconsistent."
                ),
            )
        return CodexProjectionLease(
            account_id,
            snapshot.provider_identity,
            snapshot.generation,
            snapshot.plan,
            credentials.access_token,
        )

    def open_session(
        self,
        account_id: SidekickAccountId,
        *,
        process_group: CodexProcessGroupPolicy = (
            CodexProcessGroupPolicy.ISOLATED
        ),
    ) -> CodexAppServerSession:
        """Open one bounded app server against the exact final home."""
        return CodexAppServerSession.open(
            self._capabilities,
            managed_codex_home(self._paths, account_id),
            self._environment,
            process_group=process_group,
        )

    def _read_authority(
        self,
        account_id: SidekickAccountId,
    ) -> tuple[bytes | None, bytes | None] | ProviderFailure:
        relative = str(account_id)
        try:
            auth = self._private.read_relative_bundle_file(
                relative,
                CODEX_AUTH_FILE,
            )
            config = self._private.read_relative_bundle_file(
                relative,
                CODEX_CONFIG_FILE,
            )
        except PersistenceFilesystemError as error:
            return _private_failure(
                error,
                "The managed Codex home cannot be read safely.",
            )
        return (
            None if auth is None else auth.data,
            None if config is None else config.data,
        )


def _private_failure(
    error: PersistenceFilesystemError,
    message: str,
) -> ProviderFailure:
    kind = _PERSISTENCE_FAILURE_KINDS.get(
        error.code,
        ProviderFailureKind.MALFORMED,
    )
    return ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=kind,
        message=message,
    )

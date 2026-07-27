"""Qualified native-default Claude authority read-back."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from datetime import datetime

from sidekick_usages.core.accounts.types import ProviderIdentity
from sidekick_usages.persistence.errors import (
    InvalidManagedArtifactError,
    PersistenceFilesystemError,
    UnsafeManagedFileError,
    UnsupportedFilesystemError,
)
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.providers.claude.auth.storage.errors import (
    ClaudeProtectedStorageError,
)
from sidekick_usages.providers.claude.auth.storage.keychain import (
    CLAUDE_CREDENTIAL_BYTES,
)
from sidekick_usages.providers.claude.auth.storage.models import (
    ClaudeAuthoritySnapshot,
    ClaudeCredentialObservation,
    ClaudeProtectedLogin,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
    observe_protected_claude_authority,
    protected_claude_login,
    read_protected_claude_authority,
)
from sidekick_usages.providers.claude.auth.storage.types import (
    ClaudeProtectedStorageFailure,
)
from sidekick_usages.providers.claude.managed.models import ClaudeCapabilities
from sidekick_usages.providers.claude.models import ClaudeNativeProfile
from sidekick_usages.providers.claude.process import (
    run_bounded_claude_command,
)
from sidekick_usages.providers.claude.types import (
    ClaudeCommandRunner,
    ClaudeProfile,
)


class _ClaudeNativeCredentialFiles:
    """Read one exact native-default credential file through persistence."""

    def __init__(self, profile: ClaudeNativeProfile) -> None:
        if (
            profile.config_directory.name != ".claude"
            or ".." in profile.config_directory.parts
        ):
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.UNSAFE
            )
        self._profile = profile
        try:
            self._filesystem = PersistenceFilesystem(
                profile.config_directory / CLAUDE_CREDENTIAL_FILE
            )
        except PersistenceFilesystemError, ValueError:
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.UNSAFE
            ) from None

    def read(self, profile: ClaudeProfile) -> bytes | None:
        """Return qualified bounded native credentials or proven absence."""
        self._require_profile(profile)
        try:
            snapshot = self._filesystem.read_provider_owned(
                CLAUDE_CREDENTIAL_BYTES
            )
        except InvalidManagedArtifactError:
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.MALFORMED
            ) from None
        except UnsafeManagedFileError, UnsupportedFilesystemError:
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.UNSAFE
            ) from None
        except PersistenceFilesystemError:
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.UNREADABLE
            ) from None
        return None if snapshot is None else snapshot.data

    def present(self, profile: ClaudeProfile) -> bool:
        """Report the exact native credential file through strict read-back."""
        return self.read(profile) is not None

    def _require_profile(self, profile: ClaudeProfile) -> None:
        if not isinstance(profile, ClaudeNativeProfile) or (
            profile != self._profile
        ):
            raise ClaudeProtectedStorageError(
                ClaudeProtectedStorageFailure.UNSAFE
            )


class ClaudeNativeAuthorityReader:
    """Compose qualified native files with strict provider read-back."""

    def __init__(self, profile: ClaudeNativeProfile) -> None:
        self._files = _ClaudeNativeCredentialFiles(profile)

    def read(
        self,
        capabilities: ClaudeCapabilities,
        reference_time: datetime,
        *,
        expected_identity: ProviderIdentity | None = None,
        environment: Mapping[str, str] | None = None,
        runner: ClaudeCommandRunner = run_bounded_claude_command,
    ) -> ClaudeAuthoritySnapshot:
        """Read native authority and bind its exact provider identity."""
        return read_protected_claude_authority(
            capabilities,
            self._files,
            reference_time,
            expected_identity=expected_identity,
            environment=environment,
            runner=runner,
        )

    def observe(
        self,
        capabilities: ClaudeCapabilities,
        reference_time: datetime,
        *,
        environment: Mapping[str, str] | None = None,
        runner: ClaudeCommandRunner = run_bounded_claude_command,
    ) -> ClaudeCredentialObservation:
        """Return real native generation even without embedded identity."""
        return observe_protected_claude_authority(
            capabilities,
            self._files,
            reference_time,
            environment=environment,
            runner=runner,
        )

    def open_login(
        self,
        capabilities: ClaudeCapabilities,
        reference_time: datetime,
        *,
        expected_identity: ProviderIdentity,
        environment: Mapping[str, str] | None = None,
        runner: ClaudeCommandRunner = run_bounded_claude_command,
    ) -> AbstractContextManager[ClaudeProtectedLogin]:
        """Open one short-lived native refresh credential lease."""
        return protected_claude_login(
            capabilities,
            self._files,
            reference_time,
            expected_identity=expected_identity,
            environment=environment,
            runner=runner,
        )

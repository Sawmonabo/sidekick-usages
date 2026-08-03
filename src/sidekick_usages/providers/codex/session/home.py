"""Qualified ownership of the neutral Codex session home."""

import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, Protocol

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import UsageError
from sidekick_usages.paths import ApplicationPaths, managed_codex_home
from sidekick_usages.providers.codex.session.config import (
    CODEX_SESSION_CONFIG_BASENAME,
    CodexSessionConfig,
)
from sidekick_usages.providers.codex.session.errors import (
    CodexSessionConfigurationError,
)
from sidekick_usages.providers.codex.session.models import (
    CODEX_SESSION_OPERATOR_PRECONDITION,
    CodexSessionConfigurationReason,
    CodexSessionPreparationReport,
)

_PRIVATE_DIRECTORY_MODE = 0o700
_PACKAGES_BASENAME = "packages"
_HOME_RECOVERY = (
    CODEX_SESSION_OPERATOR_PRECONDITION,
    "Restore the Sidekick Codex session path as an owner-owned 0700 real "
    "directory, then restart Sidekick.",
)
_STATE_RECOVERY = (
    CODEX_SESSION_OPERATOR_PRECONDITION,
    "Remove all state from the neutral Codex session home, then restart "
    "Sidekick.",
)
_COLLISION_RECOVERY = (
    CODEX_SESSION_OPERATOR_PRECONDITION,
    "Restore the canonical session path outside native and saved Codex "
    "private homes, then restart Sidekick.",
)
_INSTALL_RECOVERY = (
    CODEX_SESSION_OPERATOR_PRECONDITION,
    "Install the official standalone Codex build in the native Codex home, "
    "then restart Sidekick.",
)

type CodexSessionStorageFactory = Callable[[Path], CodexSessionHomeStorage]
type CodexSessionAccountReader = Callable[[], tuple[SavedAccount, ...]]


class CodexSessionHomeStorage(Protocol):
    """Qualified filesystem operations required by the session owner."""

    def ensure_owned_directory(self, directory: Path) -> None:
        """Create or validate one qualified direct child."""

    def relative_entry_present(
        self,
        relative: str,
        basename: str,
    ) -> bool:
        """Return whether one qualified direct child contains an entry."""

    def update_owned_file(
        self,
        directory: Path,
        basename: str,
        update: Callable[[bytes | None], bytes],
    ) -> object:
        """Transform and atomically commit one exact owned file."""


def _current_user_id() -> int:
    """Return the owner identity required for the session home."""
    return os.geteuid()


def qualify_codex_session_home(
    paths: ApplicationPaths,
    storage_factory: CodexSessionStorageFactory,
    account_reader: CodexSessionAccountReader,
    *,
    native_home: Path,
    forbidden_entries: tuple[str, ...],
) -> Path:
    """Create and qualify the canonical token-free Codex session home."""
    return _CodexSessionHome(
        paths,
        storage_factory,
        account_reader,
        native_home=native_home,
        forbidden_entries=forbidden_entries,
    ).prepare()


class _CodexSessionHome:
    """Own first-use creation of the canonical neutral session home."""

    def __init__(
        self,
        paths: ApplicationPaths,
        storage_factory: CodexSessionStorageFactory,
        account_reader: CodexSessionAccountReader,
        *,
        native_home: Path,
        forbidden_entries: tuple[str, ...],
    ) -> None:
        if not paths.codex_session_home.is_absolute():
            raise ValueError("Codex session home must be absolute.")
        if not native_home.is_absolute():
            raise ValueError("Native Codex home must be absolute.")
        if not forbidden_entries or any(
            not entry or Path(entry).name != entry
            for entry in forbidden_entries
        ):
            raise ValueError("Forbidden session entries are invalid.")
        self._paths = paths
        self._home = paths.codex_session_home
        self._native_home = native_home
        self._forbidden_entries = forbidden_entries
        self._storage_factory = storage_factory
        self._account_reader = account_reader

    def prepare(self) -> Path:
        """Create and qualify the token-free neutral session home."""
        credential_state = (
            CodexSessionConfigurationReason
        ).CREDENTIAL_STATE_PRESENT
        try:
            tree = self._storage_factory(self._home.parent)
            self._require_canonical_separation()
            if self._home.resolve(strict=False) != self._home:
                self._refuse(
                    CodexSessionConfigurationReason.HOME_UNSAFE,
                    _HOME_RECOVERY,
                )
            tree.ensure_owned_directory(self._home)
            metadata = self._home.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or metadata.st_uid != _current_user_id()
                or stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE
                or self._home.resolve(strict=True) != self._home
            ):
                self._refuse(
                    CodexSessionConfigurationReason.HOME_UNSAFE,
                    _HOME_RECOVERY,
                )
            for basename in self._forbidden_entries:
                if tree.relative_entry_present(self._home.name, basename):
                    self._refuse(
                        credential_state,
                        _STATE_RECOVERY,
                    )
            tree.update_owned_file(
                self._home,
                CODEX_SESSION_CONFIG_BASENAME,
                CodexSessionConfig(self._home).prepare,
            )
            self._ensure_package_projection()
        except CodexSessionConfigurationError:
            raise
        except UsageError, OSError, RuntimeError:
            self._refuse(
                CodexSessionConfigurationReason.HOME_UNSAFE,
                _HOME_RECOVERY,
            )
        return self._home

    def _ensure_package_projection(self) -> None:
        source = self._native_home / _PACKAGES_BASENAME
        projected = self._home / _PACKAGES_BASENAME
        try:
            source_status = source.lstat()
            source_resolved = source.resolve(strict=True)
            if (
                stat.S_ISLNK(source_status.st_mode)
                or not stat.S_ISDIR(source_status.st_mode)
                or source_status.st_uid != _current_user_id()
                or stat.S_IMODE(source_status.st_mode) & 0o022
                or not source_resolved.is_dir()
            ):
                self._refuse(
                    CodexSessionConfigurationReason.MANAGED_INSTALL_UNAVAILABLE,
                    _INSTALL_RECOVERY,
                )
            if not os.path.lexists(projected):
                projected.symlink_to(source, target_is_directory=True)
                self._sync_directory(self._home)
            projected_status = projected.lstat()
            if (
                not stat.S_ISLNK(projected_status.st_mode)
                or projected_status.st_uid != _current_user_id()
                or os.readlink(projected) != str(source)
                or projected.resolve(strict=True) != source_resolved
            ):
                self._refuse(
                    CodexSessionConfigurationReason.HOME_UNSAFE,
                    _HOME_RECOVERY,
                )
        except CodexSessionConfigurationError:
            raise
        except OSError, RuntimeError:
            self._refuse(
                CodexSessionConfigurationReason.MANAGED_INSTALL_UNAVAILABLE,
                _INSTALL_RECOVERY,
            )

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _require_canonical_separation(self) -> None:
        collision = (
            CodexSessionConfigurationReason
        ).PRIVATE_AUTHORITY_COLLISION
        try:
            native_home = self._native_home.resolve(strict=False)
        except OSError, RuntimeError:
            self._refuse(
                collision,
                _COLLISION_RECOVERY,
            )
        if (
            self._home == native_home
            or self._home.is_relative_to(native_home)
            or native_home.is_relative_to(self._home)
        ):
            self._refuse(
                collision,
                _COLLISION_RECOVERY,
            )
        private_root = self._paths.private_codex_profiles
        if (
            self._home == private_root
            or self._home.is_relative_to(private_root)
            or private_root.is_relative_to(self._home)
        ):
            self._refuse(
                collision,
                _COLLISION_RECOVERY,
            )
        for account in self._account_reader():
            if (
                account.provider_id is ProviderId.CODEX
                and managed_codex_home(self._paths, account.account_id)
                == self._home
            ):
                self._refuse(
                    collision,
                    _COLLISION_RECOVERY,
                )

    @staticmethod
    def _refuse(
        reason: CodexSessionConfigurationReason,
        operator_steps: tuple[str, ...],
    ) -> NoReturn:
        raise CodexSessionConfigurationError(
            CodexSessionPreparationReport(reason, operator_steps)
        )

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
from sidekick_usages.providers.codex.broker.errors import (
    codex_session_configuration_error,
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
    "Restore the canonical session path outside saved Codex private homes, "
    "then restart Sidekick.",
)

type _StorageFactory = Callable[[Path], _CodexSessionHomeStorage]
type _AccountReader = Callable[[], tuple[SavedAccount, ...]]


class _CodexSessionHomeStorage(Protocol):
    """Qualified filesystem operations required by the session owner."""

    def ensure_owned_directory(self, directory: Path) -> None:
        """Create or validate one qualified direct child."""

    def relative_bundle_present(self, relative: str) -> bool:
        """Return whether one qualified direct child contains state."""


def _current_user_id() -> int:
    """Return the owner identity required for the session home."""
    return os.geteuid()


def prepare_codex_session_home(
    paths: ApplicationPaths,
    storage_factory: _StorageFactory,
    account_reader: _AccountReader,
) -> Path:
    """Create and qualify the canonical token-free Codex session home."""
    try:
        return _CodexSessionHome(
            paths,
            storage_factory,
            account_reader,
        ).prepare()
    except CodexSessionConfigurationError as error:
        raise codex_session_configuration_error(error) from None


class _CodexSessionHome:
    """Own first-use creation of the canonical neutral session home."""

    def __init__(
        self,
        paths: ApplicationPaths,
        storage_factory: _StorageFactory,
        account_reader: _AccountReader,
    ) -> None:
        if not paths.codex_session_home.is_absolute():
            raise ValueError("Codex session home must be absolute.")
        self._paths = paths
        self._home = paths.codex_session_home
        self._storage_factory = storage_factory
        self._account_reader = account_reader

    def prepare(self) -> Path:
        """Create and qualify the token-free neutral session home."""
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
            if tree.relative_bundle_present(self._home.name):
                self._refuse(
                    CodexSessionConfigurationReason.CREDENTIAL_STATE_PRESENT,
                    _STATE_RECOVERY,
                )
        except CodexSessionConfigurationError:
            raise
        except UsageError, OSError, RuntimeError:
            self._refuse(
                CodexSessionConfigurationReason.HOME_UNSAFE,
                _HOME_RECOVERY,
            )
        return self._home

    def _require_canonical_separation(self) -> None:
        private_root = self._paths.private_codex_profiles
        if self._home == private_root or self._home.is_relative_to(
            private_root
        ):
            self._refuse(
                CodexSessionConfigurationReason.PRIVATE_AUTHORITY_COLLISION,
                _COLLISION_RECOVERY,
            )
        for account in self._account_reader():
            if (
                account.provider_id is ProviderId.CODEX
                and managed_codex_home(self._paths, account.account_id)
                == self._home
            ):
                self._refuse(
                    CodexSessionConfigurationReason.PRIVATE_AUTHORITY_COLLISION,
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

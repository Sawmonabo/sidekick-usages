"""Passive lifecycle ownership for private credential-refresh artifacts."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sidekick_usages.core.models import Account
from sidekick_usages.persistence.credential_refresh_stage import (
    decode_credential_refresh_stage,
)
from sidekick_usages.persistence.errors import (
    PersistenceCode,
    PersistenceError,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.inventory import OrphanedPrivateCredentials
from sidekick_usages.persistence.locking import (
    PersistenceLock,
    StoreLockedError,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.refresh import (
    JOURNAL_BASENAME,
    STAGE_BASENAME,
    credential_digest,
    credential_kind,
    decode_refresh_journal,
    label_digest,
    require_sha256,
)

_LIFECYCLE_LOCK_BASENAME = "lifecycle.lock"
_ROUTED_LOCK_SUFFIXES = (".refresh.lock", ".evidence.lock")


class CredentialRefreshRecoveryBlockedError(PersistenceError):
    """Private refresh evidence cannot be resolved automatically."""

    def __init__(self) -> None:
        self.code = PersistenceCode.INTERRUPTED_ARTIFACTS
        self.next_command = ("sidekick-usages", "doctor")
        super().__init__(
            "Credential refresh recovery is blocked; run "
            "`sidekick-usages doctor`."
        )


class CredentialRefreshActiveError(PersistenceError):
    """A provider refresh operation currently holds a hard lock."""

    def __init__(self) -> None:
        self.code = PersistenceCode.STORE_LOCKED
        super().__init__(
            "A credential refresh is active; retry after it completes."
        )


class CredentialRefreshTargetUnavailableError(PersistenceError):
    """The durable target disappeared or no longer rotates."""

    def __init__(self, account: Account | None) -> None:
        """Retain the exact fresh authority that ended stabilization."""
        self.account = account
        self.code = PersistenceCode.SOURCE_CHANGED
        super().__init__("The credential refresh target is unavailable.")


class CredentialRefreshUnstableError(PersistenceError):
    """Durable target authority did not stabilize within the bound."""

    def __init__(self, account: Account | None) -> None:
        """Retain the last fresh authority observed within the bound."""
        self.account = account
        self.code = PersistenceCode.SOURCE_CHANGED
        super().__init__("The credential refresh target kept changing.")


class CredentialRefreshStateKind(StrEnum):
    """Closed passive states for the private refresh root."""

    CLEAN = "clean"
    RECOVERABLE = "recoverable"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class CredentialRefreshState:
    """Secret-free passive state for doctor and lifecycle owners."""

    kind: CredentialRefreshStateKind


class CredentialRefreshArtifacts:
    """Inspect, quiesce, and destroy the private refresh root."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ValueError("Credential refresh root must be absolute.")
        self._root = root
        self._tree = PrivateCredentialTree(root)

    def assess(self) -> CredentialRefreshState:
        """Return clean, locally recoverable, or fail-closed state."""
        try:
            _require_direct_file_namespace(self._tree.list_owned_files())
            directories = self._tree.list_owned_directories()
            if not directories:
                return CredentialRefreshState(CredentialRefreshStateKind.CLEAN)
            for directory in directories:
                self._inspect_directory(directory)
        except PersistenceError, OSError, ValueError:
            return CredentialRefreshState(CredentialRefreshStateKind.BLOCKED)
        return CredentialRefreshState(CredentialRefreshStateKind.RECOVERABLE)

    def require_quiescent(self) -> None:
        """Reject any currently held operation or evidence hard lock."""
        try:
            files = self._tree.list_owned_files()
            _require_direct_file_namespace(files)
            for path in files:
                if not path.name.endswith((".refresh.lock", ".evidence.lock")):
                    continue
                authority = path.with_name(path.name.removesuffix(".lock"))
                try:
                    with PersistenceLock(
                        PersistenceFilesystem(authority),
                        timeout_seconds=0,
                    ).hold():
                        pass
                except StoreLockedError:
                    raise CredentialRefreshActiveError from None
        except CredentialRefreshActiveError:
            raise
        except PersistenceError, OSError, ValueError:
            raise CredentialRefreshRecoveryBlockedError from None

    @contextmanager
    def hold_quiescent(self) -> Iterator[None]:
        """Exclude new refreshes while a lifecycle mutation is active."""
        lifecycle = PersistenceLock(
            PersistenceFilesystem(self._root / "lifecycle")
        )
        with lifecycle.hold():
            self.require_quiescent()
            yield

    def destroy_all(self) -> None:
        """Delete every validated refresh secret, journal, and sidecar."""
        try:
            self._tree.destroy_all()
            observed = self._tree.observe()
        except PersistenceError:
            raise CredentialRefreshRecoveryBlockedError from None
        if observed is not OrphanedPrivateCredentials.ABSENT:
            raise CredentialRefreshRecoveryBlockedError

    def destroy_transactions(self) -> None:
        """Delete and prove absence of staged secrets and journals only."""
        try:
            _require_direct_file_namespace(self._tree.list_owned_files())
            directories = self._tree.list_owned_directories()
            for directory in directories:
                self._tree.destroy_owned_directory(directory)
            if self._tree.list_owned_directories():
                raise CredentialRefreshRecoveryBlockedError
            _require_direct_file_namespace(self._tree.list_owned_files())
        except PersistenceError:
            raise CredentialRefreshRecoveryBlockedError from None

    def _inspect_directory(self, directory: Path) -> None:
        try:
            require_sha256(directory.name)
        except ValueError:
            raise CredentialRefreshRecoveryBlockedError from None
        journal_snapshot = self._tree.read_owned_file(
            directory,
            JOURNAL_BASENAME,
        )
        if journal_snapshot is None:
            raise CredentialRefreshRecoveryBlockedError
        journal = decode_refresh_journal(journal_snapshot.data)
        if (
            journal.account_label_digest != directory.name
            or journal.stage_state == "durability_uncertain"
        ):
            raise CredentialRefreshRecoveryBlockedError
        stage = self._tree.read_owned_file(directory, STAGE_BASENAME)
        if stage is None:
            if journal.stage_state != "intent":
                raise CredentialRefreshRecoveryBlockedError
            return
        staged = decode_credential_refresh_stage(stage.data).account
        staged_digest = credential_digest(staged.credentials)
        if (
            label_digest(staged.label) != journal.account_label_digest
            or staged.provider_id.value != journal.provider_id
            or credential_kind(staged.credentials)
            != journal.expected_credential_kind
            or (
                journal.staged_credential_sha256 is not None
                and journal.staged_credential_sha256 != staged_digest
            )
        ):
            raise CredentialRefreshRecoveryBlockedError


def _require_direct_file_namespace(files: tuple[Path, ...]) -> None:
    """Accept only lifecycle and digest-routed lock sidecars."""
    for path in files:
        if path.name == _LIFECYCLE_LOCK_BASENAME:
            continue
        for suffix in _ROUTED_LOCK_SUFFIXES:
            if path.name.endswith(suffix):
                require_sha256(path.name.removesuffix(suffix))
                break
        else:
            raise CredentialRefreshRecoveryBlockedError


__all__ = [
    "CredentialRefreshActiveError",
    "CredentialRefreshArtifacts",
    "CredentialRefreshRecoveryBlockedError",
    "CredentialRefreshState",
    "CredentialRefreshStateKind",
    "CredentialRefreshTargetUnavailableError",
    "CredentialRefreshUnstableError",
]

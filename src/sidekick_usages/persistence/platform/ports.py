"""Native persistence operation ports."""

from pathlib import Path
from typing import IO, Protocol

from sidekick_usages.persistence.platform.models import NativeFile
from sidekick_usages.persistence.platform.types import FilesystemFamily


class NativePlatform(Protocol):
    """Native operations required by one account-file transaction."""

    def qualify(self, parent: Path) -> FilesystemFamily:
        """Return the approved filesystem family for ``parent``."""

    def ensure_parent(self, parent: Path) -> None:
        """Create or validate the private Sidekick-owned parent."""

    def repair_parent_permissions(self, parent: Path) -> bool:
        """Harden one preflight-safe released parent directory."""

    def list_basenames(self, parent: Path) -> tuple[str, ...]:
        """List sibling basenames without opening their contents."""

    def read(
        self,
        parent: Path,
        basename: str,
        limit: int,
    ) -> NativeFile | None:
        """No-follow read one protected regular object when present."""

    def create_private(
        self,
        parent: Path,
        basename: str,
        data: bytes,
    ) -> NativeFile:
        """Create, synchronize, reopen, and verify one private file."""

    def publish_no_replace(
        self,
        parent: Path,
        temporary_basename: str,
        final_basename: str,
        device: int,
        inode: int,
    ) -> None:
        """Atomically publish an immutable file without replacement."""

    def replace(
        self,
        parent: Path,
        temporary_basename: str,
        final_basename: str,
        *,
        destination_exists: bool,
        device: int,
        inode: int,
    ) -> None:
        """Atomically commit the authoritative candidate."""

    def harden(
        self,
        parent: Path,
        final_basename: str,
        limit: int,
    ) -> None:
        """Request native post-publication durability for final state."""

    def harden_cleanup(self, parent: Path) -> None:
        """Request native namespace durability after temporary cleanup."""

    def remove_candidate(
        self,
        parent: Path,
        basename: str,
    ) -> bool:
        """Remove one exact owned temporary candidate if it remains."""

    def remove_validated(
        self,
        parent: Path,
        basename: str,
        device: int,
        inode: int,
    ) -> bool:
        """Remove an exact basename only while identity still matches."""

    def open_lock(self, parent: Path, basename: str) -> IO[bytes]:
        """Securely create or open the persistent lock sidecar."""

    def prove_lock_identity(
        self,
        parent: Path,
        basename: str,
        sidecar: IO[bytes],
    ) -> None:
        """Prove the locked handle still names the exact sidecar path."""

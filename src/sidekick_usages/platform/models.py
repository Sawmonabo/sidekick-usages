"""Operating-system boundary models."""

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Kernel process ID paired with its non-reusable start identity."""

    process_id: int
    start_identity: int

    def __post_init__(self) -> None:
        """Require positive kernel process facts."""
        if any(
            type(value) is not int or value <= 0
            for value in (self.process_id, self.start_identity)
        ):
            raise ValueError("Process identity is invalid.")


@dataclass(frozen=True, slots=True)
class PeerIdentity:
    """Verified operating-system identity for one local connection."""

    effective_user_id: int
    process_identity: ProcessIdentity | None = None

    def __post_init__(self) -> None:
        """Require a nonnegative effective user identifier."""
        if (
            isinstance(self.effective_user_id, bool)
            or not isinstance(self.effective_user_id, int)
            or self.effective_user_id < 0
        ):
            raise ValueError("Peer effective user ID is invalid.")


@dataclass(frozen=True, slots=True)
class ExecutableProvenance:
    """Immutable identity of one qualified executable."""

    path: Path
    device: int
    inode: int
    size: int
    modified_nanoseconds: int

    def __post_init__(self) -> None:
        """Require an absolute path and valid file identity."""
        if not self.path.is_absolute():
            raise ValueError("Executable provenance path must be absolute.")
        if (
            min(
                self.device,
                self.inode,
                self.size,
                self.modified_nanoseconds,
            )
            < 0
        ):
            raise ValueError("Executable provenance is invalid.")

    @classmethod
    def from_stat(
        cls,
        path: Path,
        file_status: os.stat_result,
    ) -> ExecutableProvenance:
        """Capture one executable's exact filesystem identity."""
        return cls(
            path=path,
            device=file_status.st_dev,
            inode=file_status.st_ino,
            size=file_status.st_size,
            modified_nanoseconds=file_status.st_mtime_ns,
        )

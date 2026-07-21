"""Validated immutable inputs for private credential bundle writes."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from sidekick_usages.persistence.artifacts import (
    require_portable_unique_basenames,
    require_safe_basename,
)

MAX_PRIVATE_FILES = 16
MAX_PRIVATE_FILE_BYTES = 1024 * 1024
MAX_PRIVATE_BUNDLE_BYTES = 4 * 1024 * 1024


def _validated_private_payloads(
    files: Mapping[str, bytes],
    expected_files: Mapping[str, bytes | None],
) -> tuple[dict[str, bytes], dict[str, bytes | None]]:
    owned_files = dict(files)
    owned_expected = dict(expected_files)
    if not owned_files or len(owned_files) > MAX_PRIVATE_FILES:
        raise ValueError("Private credential file count is unsupported.")
    require_portable_unique_basenames(owned_files)
    total = 0
    for basename, payload in owned_files.items():
        require_safe_basename(basename)
        if not isinstance(payload, bytes):
            raise TypeError("Private credential payloads must be bytes.")
        if len(payload) > MAX_PRIVATE_FILE_BYTES:
            raise ValueError("A private credential file is too large.")
        total += len(payload)
    if total > MAX_PRIVATE_BUNDLE_BYTES:
        raise ValueError("Private credential bundle is too large.")
    for basename, payload in owned_expected.items():
        require_safe_basename(basename)
        if payload is not None and not isinstance(payload, bytes):
            raise TypeError("Expected private payloads must be bytes.")
        if payload is not None and len(payload) > MAX_PRIVATE_FILE_BYTES:
            raise ValueError(
                "An expected private credential file is too large."
            )
    if not owned_expected.keys() <= owned_files.keys():
        raise ValueError("Expected files must belong to the prepared bundle.")
    return owned_files, owned_expected


@dataclass(frozen=True, slots=True)
class PreparedPrivateBundleWrite:
    """Secret-safe immutable input for one coordinated private write."""

    path: Path
    files: Mapping[str, bytes] = field(repr=False)
    expected_bundle_present: bool
    expected_files: Mapping[str, bytes | None] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError(
                "Private credential bundle path must be absolute."
            )
        require_safe_basename(self.path.name)
        if type(self.expected_bundle_present) is not bool:
            raise TypeError("expected_bundle_present must be Boolean.")
        files, expected = _validated_private_payloads(
            self.files,
            self.expected_files,
        )
        object.__setattr__(self, "files", MappingProxyType(files))
        object.__setattr__(self, "expected_files", MappingProxyType(expected))


__all__ = [
    "MAX_PRIVATE_BUNDLE_BYTES",
    "MAX_PRIVATE_FILES",
    "MAX_PRIVATE_FILE_BYTES",
    "PreparedPrivateBundleWrite",
]

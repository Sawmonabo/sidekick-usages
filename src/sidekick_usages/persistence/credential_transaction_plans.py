"""Validated journal planning for coordinated credential transactions."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sidekick_usages.persistence.artifacts import (
    portable_basename_key,
    require_portable_unique_basenames,
)
from sidekick_usages.persistence.errors import (
    PrivateCredentialCollisionError,
)
from sidekick_usages.persistence.limits import MAX_ACCOUNTS
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.private_credentials import (
    PreparedPrivateBundleWrite,
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.transaction import (
    CredentialJournal,
    CredentialSourceGuardRecord,
    CredentialTransactionFile,
    CredentialTransactionJournal,
    encode_credential_journal,
    journal_authority,
)
from sidekick_usages.persistence.types.artifact import (
    sha256_digest,
)
from sidekick_usages.persistence.types.credential import (
    PrivateCredentialOwnership,
)

__all__ = [
    "CredentialTransactionPlan",
    "PlannedCredentialFile",
    "build_runtime_transaction_plan",
    "validate_runtime_displaced",
]


@dataclass(frozen=True, slots=True)
class PlannedCredentialFile:
    """One secret-bearing file transition paired with its journal record."""

    record: CredentialTransactionFile
    target: bytes = field(repr=False)
    base: FileSnapshot | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class CredentialTransactionPlan:
    """One validated non-secret journal and its in-memory private payloads."""

    journal: CredentialJournal
    files: tuple[PlannedCredentialFile, ...] = field(repr=False)


def _require_canonical_bundle(
    tree: PrivateCredentialTree,
    path: Path,
) -> None:
    if tree.classify_bundle(path) is not PrivateCredentialOwnership.CANONICAL:
        raise ValueError("Private bundle is not canonically owned.")


def validate_runtime_displaced(
    tree: PrivateCredentialTree,
    bundles: Iterable[Path],
) -> tuple[Path, ...]:
    """Validate direct runtime bundle removals without changing state."""
    unique: dict[str, Path] = {}
    for bundle in bundles:
        _require_canonical_bundle(tree, bundle)
        if bundle.parent != tree.root:
            raise ValueError("Runtime bundle path must be a direct child.")
        key = portable_basename_key(bundle.name)
        if key in unique:
            raise ValueError("Displaced private bundle paths must be unique.")
        unique[key] = bundle
    if len(unique) > MAX_ACCOUNTS:
        raise ValueError("Too many displaced private bundles.")
    return tuple(unique[key] for key in sorted(unique))


def _runtime_file_plan(
    tree: PrivateCredentialTree,
    bundle: PreparedPrivateBundleWrite,
    next_index: int,
) -> tuple[tuple[PlannedCredentialFile, ...], int]:
    _require_canonical_bundle(tree, bundle.path)
    if bundle.path.parent != tree.root:
        raise ValueError("Runtime bundle path must be a direct child.")
    present = tree.bundle_present(bundle.path)
    if present is not bundle.expected_bundle_present:
        raise PrivateCredentialCollisionError(bundle.path.name)
    planned: list[PlannedCredentialFile] = []
    for basename, target in sorted(bundle.files.items()):
        base = tree.read_owned_file(bundle.path, basename) if present else None
        if basename in bundle.expected_files:
            expected = bundle.expected_files[basename]
            if (base is None) is not (expected is None) or (
                base is not None and base.data != expected
            ):
                raise PrivateCredentialCollisionError(bundle.path.name)
        record = CredentialTransactionFile(
            bundle_basename=bundle.path.name,
            basename=basename,
            stage_basename=f"stage-{next_index:04d}.bin",
            backup_basename=(
                f"backup-{next_index:04d}.bin" if base is not None else None
            ),
            base_sha256=(
                str(base.fingerprint.digest) if base is not None else None
            ),
            target_sha256=str(sha256_digest(target)),
        )
        planned.append(PlannedCredentialFile(record, target, base))
        next_index += 1
    return tuple(planned), next_index


def build_runtime_transaction_plan(
    tree: PrivateCredentialTree,
    payload: bytes,
    expected_source: ExpectedAuthority,
    bundles: tuple[PreparedPrivateBundleWrite, ...],
    displaced: tuple[Path, ...],
    source_guard: CredentialSourceGuardRecord | None,
) -> CredentialTransactionPlan:
    """Build the strict version-one runtime journal and payload plan."""
    if len(bundles) > MAX_ACCOUNTS:
        raise ValueError("Too many prepared private bundles.")
    bundle_names = tuple(bundle.path.name for bundle in bundles)
    require_portable_unique_basenames(bundle_names)
    planned: list[PlannedCredentialFile] = []
    records: list[CredentialTransactionFile] = []
    next_index = 0
    for bundle in sorted(bundles, key=lambda item: item.path.name):
        additions, next_index = _runtime_file_plan(
            tree,
            bundle,
            next_index,
        )
        planned.extend(additions)
        records.extend(item.record for item in additions)
    journal = CredentialTransactionJournal(
        journal_version=1,
        base_authority=journal_authority(expected_source),
        source_guard=source_guard,
        target_authority_sha256=str(sha256_digest(payload)),
        target_authority_size=len(payload),
        target_bundles=tuple(sorted(bundle_names)),
        base_present_bundles=tuple(
            sorted(
                bundle.path.name
                for bundle in bundles
                if bundle.expected_bundle_present
            )
        ),
        files=tuple(records),
        displaced_bundles=tuple(path.name for path in displaced),
    )
    encode_credential_journal(journal)
    return CredentialTransactionPlan(journal, tuple(planned))

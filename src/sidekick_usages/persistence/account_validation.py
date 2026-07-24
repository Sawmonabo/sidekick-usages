"""Heavy account codecs used only by account-specific filesystem actions."""

from sidekick_usages.persistence.artifacts import (
    AuthorityGeneration,
    ManagedArtifact,
    ManagedArtifactKind,
    Sha256Digest,
    sha256_digest,
)
from sidekick_usages.persistence.errors import (
    BackupConflictError,
    InvalidManagedArtifactError,
    PersistenceError,
    PersistenceFilesystemError,
)
from sidekick_usages.persistence.schema.account import decode_version_three
from sidekick_usages.persistence.schemas import (
    decode_authority,
    decode_generation_zero,
    decode_prototype_receipt,
    decode_version_one,
    decode_version_two,
    encode_version_one,
)

__all__ = [
    "require_prototype_receipt_digest",
    "validate_account_generation",
    "validate_account_recovery_artifact",
]


def _validate_recovery_artifact(
    artifact: ManagedArtifact,
    payload: bytes,
) -> None:
    if artifact.kind is ManagedArtifactKind.AUTHORITY:
        decode_authority(payload)
        return
    if artifact.kind is ManagedArtifactKind.PROTOTYPE_RECEIPT:
        receipt = decode_prototype_receipt(payload)
        if receipt.prototype_sha256 != artifact.digest:
            raise InvalidManagedArtifactError(artifact.basename)
        return
    if artifact.digest != sha256_digest(payload):
        raise BackupConflictError(artifact.basename)
    if artifact.kind is ManagedArtifactKind.GENERATION_ZERO_BACKUP:
        decode_generation_zero(payload)
        return
    if artifact.kind is ManagedArtifactKind.VERSION_ONE_SNAPSHOT:
        document = decode_version_one(payload)
        if encode_version_one(document) == payload:
            return
    raise BackupConflictError(artifact.basename)


def validate_account_recovery_artifact(
    artifact: ManagedArtifact,
    payload: bytes,
) -> None:
    """Validate one account-owned recovery artifact by its exact role."""
    try:
        _validate_recovery_artifact(artifact, payload)
    except PersistenceFilesystemError:
        raise
    except PersistenceError:
        if artifact.kind is ManagedArtifactKind.AUTHORITY:
            raise
        if artifact.kind is ManagedArtifactKind.PROTOTYPE_RECEIPT:
            raise InvalidManagedArtifactError(artifact.basename) from None
        raise BackupConflictError(artifact.basename) from None


def validate_account_generation(
    payload: bytes,
    generation: AuthorityGeneration,
) -> None:
    """Validate bytes against one exact account schema generation."""
    if generation is AuthorityGeneration.GENERATION_ZERO:
        decode_generation_zero(payload)
    elif generation is AuthorityGeneration.VERSION_ONE:
        decode_version_one(payload)
    elif generation is AuthorityGeneration.VERSION_TWO:
        decode_version_two(payload)
    else:
        decode_version_three(payload)


def require_prototype_receipt_digest(
    payload: bytes,
    expected: Sha256Digest,
) -> None:
    """Require one canonical prototype receipt for the expected digest."""
    receipt = decode_prototype_receipt(payload)
    if receipt.prototype_sha256 != expected:
        raise ValueError("Receipt digest does not match its basename.")

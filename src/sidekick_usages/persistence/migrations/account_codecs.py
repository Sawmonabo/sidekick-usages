"""Pure account migration and released-rollback payload policy."""

from sidekick_usages.core.models import Account
from sidekick_usages.persistence.assessment import PersistenceAssessment
from sidekick_usages.persistence.errors import (
    InvalidSchemaError,
    PersistenceCode,
    SourceChangedError,
)
from sidekick_usages.persistence.migrations.credential_kinds import (
    VersionOneCredentialClassification,
    require_migratable_version_one,
)
from sidekick_usages.persistence.observations import (
    ArtifactObservation,
    AuthorityKind,
    PersistenceObservation,
)
from sidekick_usages.persistence.schemas import (
    VersionOneDocument,
    encode_generation_zero,
    encode_version_two,
)
from sidekick_usages.persistence.transforms import (
    generation_zero_to_version_two,
    prototype_to_version_two,
    version_one_to_version_two,
    version_two_to_accounts,
    version_two_to_v060,
)

MIGRATABLE_GENERATION_ZERO = frozenset(
    {
        PersistenceCode.MIGRATION_REQUIRED,
        PersistenceCode.ROLLBACK_PREPARED,
        PersistenceCode.LEGACY_WRITER_DETECTED,
    }
)
CURRENT_VERSION_TWO = frozenset(
    {PersistenceCode.CURRENT, PersistenceCode.PROTOTYPE_IMPORTED}
)


def accounts_from_current(
    observation: PersistenceObservation,
    assessment: PersistenceAssessment,
) -> tuple[Account, ...]:
    """Return accounts only from absent or runtime-safe current state."""
    if assessment.code is PersistenceCode.EMPTY:
        if observation.authority.kind is not AuthorityKind.ABSENT:
            raise SourceChangedError
        return ()
    if assessment.code not in CURRENT_VERSION_TWO:
        raise ValueError("Assessment is not runtime-safe version two.")
    document = observation.authority.version_two
    if document is None:
        raise SourceChangedError
    return version_two_to_accounts(document)


def credential_migration_preflight(
    observation: PersistenceObservation,
) -> VersionOneCredentialClassification | None:
    """Classify legacy credentials from one passive authority inspection."""
    legacy = observation.authority.version_one
    if legacy is None and observation.authority.generation_zero is not None:
        legacy = VersionOneDocument(
            observation.authority.generation_zero.accounts
        )
    if legacy is None:
        return None
    return require_migratable_version_one(legacy)


def generation_zero_payload(observation: PersistenceObservation) -> bytes:
    """Return canonical current bytes for generation-zero evidence."""
    document = observation.authority.generation_zero
    if document is None:
        raise SourceChangedError
    return encode_version_two(generation_zero_to_version_two(document))


def version_one_payload(observation: PersistenceObservation) -> bytes:
    """Return canonical current bytes for schema-version-one evidence."""
    document = observation.authority.version_one
    if document is None:
        raise SourceChangedError
    return encode_version_two(version_one_to_version_two(document))


def rollback_payload(observation: PersistenceObservation) -> bytes:
    """Return exact released-v0.6.0 bytes for canonical current state."""
    document = observation.authority.version_two
    if document is None:
        raise SourceChangedError
    canonical = encode_version_two(document)
    if observation.authority.content != canonical:
        raise InvalidSchemaError
    return encode_generation_zero(version_two_to_v060(document))


def prototype_payload(artifact: ArtifactObservation) -> bytes:
    """Return canonical current bytes for a validated prototype."""
    document = artifact.prototype
    if document is None:
        raise InvalidSchemaError
    return encode_version_two(prototype_to_version_two(document))


__all__ = [
    "CURRENT_VERSION_TWO",
    "MIGRATABLE_GENERATION_ZERO",
    "accounts_from_current",
    "credential_migration_preflight",
    "generation_zero_payload",
    "prototype_payload",
    "rollback_payload",
    "version_one_payload",
]

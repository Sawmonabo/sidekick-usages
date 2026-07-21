"""Typed read-only account migration preview result."""

from dataclasses import dataclass

from sidekick_usages.persistence.assessment import PersistenceAssessment
from sidekick_usages.persistence.migrations.credential_kinds import (
    VersionOneCredentialClassification,
)


@dataclass(frozen=True, slots=True)
class AccountMigrationPreview:
    """One assessment and its same-inspection credential classification."""

    assessment: PersistenceAssessment
    classification: VersionOneCredentialClassification | None


__all__ = ["AccountMigrationPreview"]

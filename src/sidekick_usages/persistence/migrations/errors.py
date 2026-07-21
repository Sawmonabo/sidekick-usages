"""Typed failures for explicit persistence migration operations."""

from enum import StrEnum

from sidekick_usages.core.types import ExitCode
from sidekick_usages.errors import UsageError
from sidekick_usages.persistence.assessment import PersistenceAssessment
from sidekick_usages.persistence.errors import (
    PersistenceCode,
    PersistenceError,
)
from sidekick_usages.persistence.migrations.location import (
    LocationMigrationAssessment,
    RuntimePersistenceSelection,
    is_ready_location_selection,
    location_persistence_code,
)
from sidekick_usages.persistence.migrations.ports import (
    PrivateAuthMigrationFailure,
)
from sidekick_usages.scheduler_quiescence import (
    SchedulerQuiescenceAssessment,
)


class VerificationPhase(StrEnum):
    """Bounded released-reader verification stage."""

    PREFLIGHT = "preflight"
    POST_COMMIT = "post_commit"


class SchedulerMutationBlockedError(UsageError):
    """Installed or unassessable schedules block persistence mutation."""

    exit_code = ExitCode.SCHEDULER_ERROR

    def __init__(self, assessment: SchedulerQuiescenceAssessment) -> None:
        self.assessment = assessment
        super().__init__(
            "Scheduled Sidekick maintenance must be stopped before mutation."
        )


class PersistenceMigrationStateError(PersistenceError):
    """Observed persistence state does not authorize this transition."""

    def __init__(self, assessment: PersistenceAssessment) -> None:
        self.assessment = assessment
        self.code = assessment.code
        self.next_command = assessment.next_command
        super().__init__(assessment.message)


class PrototypeReimportRequiredError(PersistenceError):
    """A different prototype requires explicit replacement intent."""

    def __init__(self, assessment: PersistenceAssessment) -> None:
        self.assessment = assessment
        self.code = PersistenceCode.PROTOTYPE_IMPORT_REQUIRED
        self.next_command = (
            "sidekick-usages",
            "migrate",
            "accounts",
            "--reimport-prototype",
        )
        super().__init__("Prototype replacement requires explicit reimport.")


class LocationMigrationStateError(PersistenceError):
    """Location state does not authorize the requested runtime operation."""

    def __init__(
        self,
        assessment: LocationMigrationAssessment[RuntimePersistenceSelection],
    ) -> None:
        self.assessment = assessment
        self.code = (
            PersistenceCode.SOURCE_CHANGED
            if is_ready_location_selection(assessment.selection)
            else location_persistence_code(assessment.selection)
        )
        self.next_command = assessment.next_command or (
            "sidekick-usages",
            "doctor",
        )
        super().__init__(
            "Persistence locations require explicit diagnosis or migration."
        )


class PrivateAuthMigrationStateError(PersistenceError):
    """Prepared private-auth relocation failed closed without mutation."""

    def __init__(self, failure: PrivateAuthMigrationFailure) -> None:
        self.failure = failure
        self.code = PersistenceCode.BACKUP_CONFLICT
        self.next_command = ("sidekick-usages", "doctor")
        super().__init__(failure.message)


class ReleasedVerifierBoundaryError(PersistenceError):
    """An injected released-reader oracle violated its safe boundary."""

    def __init__(self, phase: VerificationPhase) -> None:
        self.phase = phase
        self.code = (
            PersistenceCode.ROLLBACK_REQUIRED
            if phase is VerificationPhase.PREFLIGHT
            else PersistenceCode.DURABILITY_UNCERTAIN
        )
        message = (
            "The released-reader verifier is unavailable before rollback."
            if phase is VerificationPhase.PREFLIGHT
            else "Committed rollback bytes could not be verified."
        )
        super().__init__(message)

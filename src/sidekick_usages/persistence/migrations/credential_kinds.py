"""Generation-one credential classification and duplicate preflight."""

from dataclasses import dataclass
from enum import StrEnum, auto

from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence import schemas as _schemas
from sidekick_usages.persistence.credential_ownership import (
    credential_ownership_conflicts,
    reject_duplicate_credential_ownership,
)
from sidekick_usages.persistence.errors import (
    PersistenceCode,
    PersistenceError,
)


class LegacyClaudeCredentialKind(StrEnum):
    """Total classification of one schema-version-one Claude record."""

    SETUP_TOKEN = auto()
    SUBSCRIPTION_LOGIN = "subscription_login"
    AMBIGUOUS = "ambiguous"


class CredentialMigrationIssueKind(StrEnum):
    """Closed secret-free generation-one migration blockers."""

    AMBIGUOUS = "ambiguous"
    DUPLICATE_ACCESS = "duplicate_access"
    DUPLICATE_REFRESH = "duplicate_refresh"


@dataclass(frozen=True, slots=True)
class LegacyClaudeRecordClassification:
    """One label and its total legacy Claude classification."""

    label: str
    kind: LegacyClaudeCredentialKind


@dataclass(frozen=True, slots=True)
class CredentialMigrationIssue:
    """One secret-free migration blocker and exact operator actions."""

    kind: CredentialMigrationIssueKind
    labels: tuple[str, ...]
    next_commands: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class VersionOneCredentialClassification:
    """Complete credential preflight for one version-one document."""

    claude_records: tuple[LegacyClaudeRecordClassification, ...]
    issues: tuple[CredentialMigrationIssue, ...]

    @property
    def setup_count(self) -> int:
        """Return the number of deterministic setup-token records."""
        return sum(
            item.kind is LegacyClaudeCredentialKind.SETUP_TOKEN
            for item in self.claude_records
        )

    @property
    def login_count(self) -> int:
        """Return the number of deterministic subscription logins."""
        return sum(
            item.kind is LegacyClaudeCredentialKind.SUBSCRIPTION_LOGIN
            for item in self.claude_records
        )

    @property
    def refresh_expiry_unavailable_count(self) -> int:
        """Return migrated logins whose old generation has no lifetime."""
        return self.login_count


class CredentialMigrationPreflightError(PersistenceError):
    """Generation-one credential ownership needs explicit repair."""

    def __init__(
        self,
        classification: VersionOneCredentialClassification,
    ) -> None:
        self.classification = classification
        self.code = PersistenceCode.MIGRATION_REQUIRED
        self.next_commands = tuple(
            command
            for issue in classification.issues
            for command in issue.next_commands
        )
        labels = tuple(
            dict.fromkeys(
                label
                for issue in classification.issues
                for label in issue.labels
            )
        )
        super().__init__(
            "Account credentials require explicit repair: "
            + ", ".join(labels)
            + "."
        )


def _repair_commands(labels: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    removals = tuple(("sidekick-usages", "remove", label) for label in labels)
    return (*removals, ("sidekick-usages", "migrate", "accounts"))


def _classify_claude(
    record: _schemas.StoredAccountRecord,
) -> LegacyClaudeCredentialKind:
    has_refresh = record.refresh_token is not None
    has_expiry = record.expires_at is not None
    has_profile = record.scopes is not None and "user:profile" in record.scopes
    has_login_only_metadata = (
        record.credential_kind is not None
        or record.refresh_expires_at is not None
        or record.claude_identity is not None
    )
    if (
        has_refresh
        and has_expiry
        and has_profile
        and not has_login_only_metadata
    ):
        return LegacyClaudeCredentialKind.SUBSCRIPTION_LOGIN
    if not (
        has_refresh or has_expiry or has_profile or has_login_only_metadata
    ):
        return LegacyClaudeCredentialKind.SETUP_TOKEN
    return LegacyClaudeCredentialKind.AMBIGUOUS


def duplicate_credential_issues(
    records: tuple[_schemas.StoredAccountRecord, ...],
) -> tuple[CredentialMigrationIssue, ...]:
    """Return exact duplicate ownership without deriving token material."""
    return tuple(
        CredentialMigrationIssue(
            (
                CredentialMigrationIssueKind.DUPLICATE_ACCESS
                if conflict.credential_field == "access_token"
                else CredentialMigrationIssueKind.DUPLICATE_REFRESH
            ),
            conflict.labels,
            _repair_commands(conflict.labels[1:]),
        )
        for conflict in credential_ownership_conflicts(records)
    )


def classify_version_one(
    document: _schemas.VersionOneDocument,
) -> VersionOneCredentialClassification:
    """Return a total secret-free classification of version-one state."""
    records = tuple(
        LegacyClaudeRecordClassification(
            str(record.label),
            _classify_claude(record),
        )
        for record in document.accounts
        if record.provider_id is ProviderId.CLAUDE
    )
    ambiguous = tuple(
        CredentialMigrationIssue(
            CredentialMigrationIssueKind.AMBIGUOUS,
            (record.label,),
            _repair_commands((record.label,)),
        )
        for record in records
        if record.kind is LegacyClaudeCredentialKind.AMBIGUOUS
    )
    duplicates = duplicate_credential_issues(document.accounts)
    return VersionOneCredentialClassification(
        records,
        (*ambiguous, *duplicates),
    )


def require_migratable_version_one(
    document: _schemas.VersionOneDocument,
) -> VersionOneCredentialClassification:
    """Return a migration preflight when no explicit repair is needed."""
    classification = classify_version_one(document)
    if classification.issues:
        raise CredentialMigrationPreflightError(classification)
    return classification


__all__ = [
    "CredentialMigrationIssue",
    "CredentialMigrationIssueKind",
    "CredentialMigrationPreflightError",
    "LegacyClaudeCredentialKind",
    "LegacyClaudeRecordClassification",
    "VersionOneCredentialClassification",
    "classify_version_one",
    "duplicate_credential_issues",
    "reject_duplicate_credential_ownership",
    "require_migratable_version_one",
]

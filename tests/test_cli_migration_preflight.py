"""Public CLI contracts for account credential migration preflight."""

from pathlib import Path

from sidekick_usages.core.types import ExitCode
from sidekick_usages.persistence.errors import PersistenceCode
from sidekick_usages.persistence.migrations.credential_kinds import (
    CredentialMigrationIssue,
    CredentialMigrationIssueKind,
    CredentialMigrationPreflightError,
    LegacyClaudeCredentialKind,
    LegacyClaudeRecordClassification,
    VersionOneCredentialClassification,
)
from tests.test_cli_persistence import (
    RecordingPersistence,
    _assessment,
    _install_context,
)


def test_migrate_accounts_previews_credential_counts_before_confirmation(
    tmp_path: Path,
) -> None:
    """The public preview is read-only and reports secret-safe counts."""
    assessment = _assessment(
        tmp_path,
        PersistenceCode.MIGRATION_REQUIRED,
        count=4,
        next_command=("sidekick-usages", "migrate", "accounts"),
    )
    classification = VersionOneCredentialClassification(
        (
            LegacyClaudeRecordClassification(
                "setup-one",
                LegacyClaudeCredentialKind.SETUP_TOKEN,
            ),
            LegacyClaudeRecordClassification(
                "setup-two",
                LegacyClaudeCredentialKind.SETUP_TOKEN,
            ),
            LegacyClaudeRecordClassification(
                "login-one",
                LegacyClaudeCredentialKind.SUBSCRIPTION_LOGIN,
            ),
        ),
        (),
    )
    persistence = RecordingPersistence(
        assessment,
        classification=classification,
    )
    harness, stdout, _ = _install_context(tmp_path, persistence)

    result = harness.invoke(["migrate", "accounts"], input_text="n\n")

    assert result.exit_code == ExitCode.MANUAL_ACTION
    assert persistence.events == ["account-preview"]
    output = stdout.getvalue()
    assert "Claude setup-token records: 2" in output
    assert "Claude subscription-login records: 1" in output
    assert "Refresh expiry unavailable: 1" in output


def test_migrate_accounts_renders_every_blocked_preflight_action(
    tmp_path: Path,
) -> None:
    """Blocked credential state reports every exact action without mutation."""
    assessment = _assessment(
        tmp_path,
        PersistenceCode.MIGRATION_REQUIRED,
        count=2,
    )
    persistence = RecordingPersistence(
        assessment,
        preview_error=CredentialMigrationPreflightError(
            VersionOneCredentialClassification(
                (
                    LegacyClaudeRecordClassification(
                        "needs-repair",
                        LegacyClaudeCredentialKind.AMBIGUOUS,
                    ),
                ),
                (
                    CredentialMigrationIssue(
                        CredentialMigrationIssueKind.AMBIGUOUS,
                        ("needs-repair",),
                        (
                            (
                                "sidekick-usages",
                                "remove",
                                "needs-repair",
                            ),
                            ("sidekick-usages", "migrate", "accounts"),
                        ),
                    ),
                    CredentialMigrationIssue(
                        CredentialMigrationIssueKind.DUPLICATE_ACCESS,
                        ("kept", "duplicate"),
                        (
                            ("sidekick-usages", "remove", "duplicate"),
                            ("sidekick-usages", "migrate", "accounts"),
                        ),
                    ),
                ),
            )
        ),
    )
    harness, _, stderr = _install_context(tmp_path, persistence)

    result = harness.invoke(["migrate", "accounts"])

    assert result.exit_code == ExitCode.MANUAL_ACTION
    assert persistence.events == ["account-preview"]
    assert stderr.getvalue().splitlines() == [
        "Account credentials require explicit repair: needs-repair, kept, "
        "duplicate.",
        "Next: sidekick-usages remove needs-repair",
        "Next: sidekick-usages migrate accounts",
        "Next: sidekick-usages remove duplicate",
        "Next: sidekick-usages migrate accounts",
    ]

"""Shared validation and projection helpers for the account-store facade."""

from collections.abc import Collection, Iterable
from dataclasses import replace
from pathlib import Path

from sidekick_usages.core.models import Account
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.assessment import (
    PersistenceAssessment,
    PersistenceIssue,
    PersistenceObservation,
)
from sidekick_usages.persistence.errors import (
    DuplicateKeyError,
    FutureSchemaError,
    InvalidSchemaError,
    MalformedJsonError,
    PersistenceCode,
    PersistenceError,
    PrivateCredentialCollisionError,
)
from sidekick_usages.persistence.observations import (
    ArtifactKind,
    ArtifactState,
    AuthorityKind,
)
from sidekick_usages.persistence.private_bundle_references import (
    canonical_private_accounts,
)
from sidekick_usages.persistence.private_bundle_writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)


class AccountStoreStateError(PersistenceError):
    """A complete passive assessment blocks runtime store use."""

    def __init__(self, assessment: PersistenceAssessment) -> None:
        self.assessment = assessment
        self.code = assessment.code
        self.next_command = assessment.next_command
        super().__init__(assessment.message)


def copy_account(
    account: Account,
    *,
    label: AccountLabel | None = None,
) -> Account:
    """Return an independently mutable copy of one runtime account."""
    resets = account.heartbeat_window_resets
    return Account(
        label=account.label if label is None else label,
        credentials=account.credentials,
        plan=account.plan,
        last_refresh_at=account.last_refresh_at,
        last_refresh_status=account.last_refresh_status,
        last_refresh_error=account.last_refresh_error,
        heartbeat_enabled=account.heartbeat_enabled,
        heartbeat_5h_reset_at=account.heartbeat_5h_reset_at,
        heartbeat_window_resets=(
            dict(resets.items()) if resets is not None else None
        ),
        heartbeat_targets=account.heartbeat_targets,
        last_heartbeat_at=account.last_heartbeat_at,
        last_heartbeat_status=account.last_heartbeat_status,
        last_heartbeat_error=account.last_heartbeat_error,
    )


def index_accounts(accounts: tuple[Account, ...]) -> dict[str, Account]:
    """Index validated accounts while preserving insertion order."""
    return {str(account.label): account for account in accounts}


def path_text(path: Path) -> str:
    """Return deterministic lexical ordering text for one private path."""
    return str(path)


def generate_account_label(
    provider_id: ProviderId,
    plan: str,
    existing: Collection[str],
) -> AccountLabel:
    """Return the smallest unused provider-plan label."""
    plan_component = (plan or "account").lower().replace(" ", "-")
    base = f"{provider_id}-{plan_component}"
    suffix = 1
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return AccountLabel(f"{base}-{suffix}")


def displaced_private_bundles(
    current: Iterable[Account],
    staged: Iterable[Account],
    private: PrivateCredentialTree,
    bundles: tuple[PreparedPrivateBundleWrite, ...],
) -> tuple[Path, ...]:
    """Validate private transitions and return unreferenced old bundles."""
    old_accounts = canonical_private_accounts(current, private)
    new_accounts = canonical_private_accounts(staged, private)
    old_references = set(old_accounts)
    new_references = set(new_accounts)
    prepared_paths = {bundle.path for bundle in bundles}
    if not prepared_paths <= new_references:
        raise ValueError(
            "Prepared private bundles must be referenced by accounts."
        )
    introduced = new_references - old_references
    if not introduced <= prepared_paths:
        unproven = min(introduced - prepared_paths, key=path_text)
        raise PrivateCredentialCollisionError(unproven.name)
    changed = {
        path
        for path in old_references & new_references
        if old_accounts[path].credentials != new_accounts[path].credentials
    }
    if not changed <= prepared_paths:
        unproven = min(changed - prepared_paths, key=path_text)
        raise PrivateCredentialCollisionError(unproven.name)
    removed = old_references - new_references
    return tuple(sorted(removed, key=path_text))


def require_store_assessment(assessment: PersistenceAssessment) -> None:
    """Require an assessment usable by the legacy runtime store."""
    if assessment.code in {
        PersistenceCode.EMPTY,
        PersistenceCode.CURRENT,
        PersistenceCode.PROTOTYPE_IMPORTED,
    }:
        return
    if assessment.code is PersistenceCode.DUPLICATE_KEY:
        raise DuplicateKeyError
    if assessment.code is PersistenceCode.MALFORMED_JSON:
        raise MalformedJsonError
    if (
        assessment.code is PersistenceCode.FUTURE_SCHEMA
        and assessment.schema_version is not None
    ):
        raise FutureSchemaError(assessment.schema_version)
    if assessment.code is PersistenceCode.INVALID_SCHEMA:
        raise InvalidSchemaError
    raise AccountStoreStateError(assessment)


def require_managed_store_assessment(
    observation: PersistenceObservation,
    assessment: PersistenceAssessment,
) -> None:
    """Accept only absent or schema-version-three runtime authority."""
    if assessment.code is PersistenceCode.EMPTY:
        return
    if (
        assessment.code is PersistenceCode.CURRENT
        and observation.authority.kind is AuthorityKind.VERSION_THREE
    ):
        return
    if observation.authority.kind is AuthorityKind.VERSION_TWO:
        migration = replace(
            assessment,
            code=PersistenceCode.MIGRATION_REQUIRED,
            write_blocked=True,
            next_command=("sidekick-usages", "migrate", "accounts"),
            message="Account data requires managed-authority migration.",
            issues=(
                PersistenceIssue(
                    PersistenceCode.MIGRATION_REQUIRED,
                    None,
                    "Account data requires managed-authority migration.",
                ),
            ),
        )
        raise AccountStoreStateError(migration)
    require_store_assessment(assessment)


def baseline_matches(
    baseline: ExpectedAuthority,
    observed: FileSnapshot | None,
) -> bool:
    """Return whether fresh authority evidence matches a loaded baseline."""
    if baseline is AuthorityExpectation.ABSENT:
        return observed is None
    return observed is not None and observed.fingerprint == baseline


def private_recovery_is_only_blocker(
    observation: PersistenceObservation,
    assessment: PersistenceAssessment,
) -> bool:
    """Allow loading only for one recoverable private transaction journal."""
    if (
        not observation.interrupted_credentials
        or assessment.code is not PersistenceCode.INTERRUPTED_ARTIFACTS
        or observation.authority.kind
        not in {
            AuthorityKind.ABSENT,
            AuthorityKind.VERSION_TWO,
            AuthorityKind.VERSION_THREE,
        }
    ):
        return False
    if any(
        artifact.kind is ArtifactKind.TEMPORARY
        or artifact.state is not ArtifactState.VALID
        for artifact in observation.artifacts
    ):
        return False
    return all(
        issue.code
        in {
            PersistenceCode.EMPTY,
            PersistenceCode.INTERRUPTED_ARTIFACTS,
            PersistenceCode.CURRENT,
            PersistenceCode.PROTOTYPE_IMPORTED,
        }
        and (
            issue.code is not PersistenceCode.INTERRUPTED_ARTIFACTS
            or issue.artifact_basename is None
        )
        for issue in assessment.issues
    )


__all__ = [
    "AccountStoreStateError",
    "baseline_matches",
    "copy_account",
    "displaced_private_bundles",
    "generate_account_label",
    "index_accounts",
    "path_text",
    "private_recovery_is_only_blocker",
    "require_managed_store_assessment",
    "require_store_assessment",
]

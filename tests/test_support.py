"""Shared deterministic test dependencies."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sidekick_usages.core.models import Account
from sidekick_usages.paths import (
    AccountLocations,
    ApplicationPaths,
    PrivateCodexLocations,
)
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)

REFERENCE_TIME = datetime(2026, 6, 12, 12, 34, 56, 789000, tzinfo=UTC)


def make_application_paths(root: Path) -> ApplicationPaths:
    """Build isolated Sidekick-owned locations below ``root``."""
    account_file = root / "accounts.json"
    private_codex_root = root / "sidekick-codex-cache"
    return ApplicationPaths(
        accounts=AccountLocations(
            canonical=account_file,
            existing_sidekick=account_file,
            prototype_cc_usage=root / "prototype" / "accounts.json",
        ),
        private_codex=PrivateCodexLocations(
            canonical=private_codex_root,
            existing_sidekick=private_codex_root,
        ),
        lifetime_cache_file=root / "codex-lifetime-cache.json",
    )


def make_account_store(
    root: Path,
    accounts: Iterable[Account] = (),
) -> AccountStore:
    """Build a loaded transactional store with a live private observer."""
    paths = make_application_paths(root)
    PersistenceFilesystem(paths.accounts.canonical).repair_parent_permissions()
    private_credentials = PrivateCredentialTree(
        paths.private_codex.canonical,
        account_path=paths.accounts.canonical,
        existing_root=paths.private_codex.existing_sidekick,
    )
    store = AccountStore(
        paths.accounts,
        orphaned_credentials_observer=private_credentials.observe,
    ).load()
    for account in accounts:
        store.persist(account)
    return store


@dataclass(slots=True)
class FixedClock:
    """Return one fixed instant while counting wall-time acquisitions."""

    value: datetime = REFERENCE_TIME
    calls: int = 0

    def now(self) -> datetime:
        """Return the fixed instant and record one acquisition."""
        self.calls += 1
        return self.value

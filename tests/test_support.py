"""Shared deterministic test dependencies."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sidekick_usages.paths import (
    AccountLocations,
    ApplicationPaths,
    PrivateCodexLocations,
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


@dataclass(slots=True)
class FixedClock:
    """Return one fixed instant while counting wall-time acquisitions."""

    value: datetime = REFERENCE_TIME
    calls: int = 0

    def now(self) -> datetime:
        """Return the fixed instant and record one acquisition."""
        self.calls += 1
        return self.value

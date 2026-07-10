"""Sidekick-owned application path discovery."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AccountLocations:
    """Locations participating in account-store compatibility.

    :ivar canonical: Selected account-store location.
    :ivar existing_sidekick: Existing Sidekick account-store location.
    :ivar prototype_cc_usage: Import-only cc-usage prototype location.
    """

    canonical: Path
    existing_sidekick: Path
    prototype_cc_usage: Path


@dataclass(frozen=True, slots=True)
class PrivateCodexLocations:
    """Sidekick-owned private Codex credential locations.

    :ivar canonical: Selected private Codex root.
    :ivar existing_sidekick: Existing Sidekick private Codex root.
    """

    canonical: Path
    existing_sidekick: Path


@dataclass(frozen=True, slots=True)
class ApplicationPaths:
    """Sidekick-owned paths resolved for one application invocation.

    :ivar accounts: Account-store compatibility locations.
    :ivar private_codex: Private Codex credential locations.
    :ivar lifetime_cache_file: Sidekick-owned Codex lifetime cache.
    """

    accounts: AccountLocations
    private_codex: PrivateCodexLocations
    lifetime_cache_file: Path


def discover_application_paths() -> ApplicationPaths:
    """Discover compatibility paths without filesystem side effects.

    :return: Sidekick-owned paths for the current user.
    """
    home = Path.home()
    sidekick_root = home / ".config" / "sidekick-usages"
    account_file = sidekick_root / "accounts.json"
    private_codex_root = sidekick_root / "codex"
    return ApplicationPaths(
        accounts=AccountLocations(
            canonical=account_file,
            existing_sidekick=account_file,
            prototype_cc_usage=(
                home / ".config" / "cc-usage" / "accounts.json"
            ),
        ),
        private_codex=PrivateCodexLocations(
            canonical=private_codex_root,
            existing_sidekick=private_codex_root,
        ),
        lifetime_cache_file=(sidekick_root / "codex-lifetime-cache.json"),
    )

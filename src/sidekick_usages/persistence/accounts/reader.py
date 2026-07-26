"""Passive saved-account metadata reader."""

from pathlib import Path

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.persistence.filesystem.reader import PrivateFileReader
from sidekick_usages.persistence.models.account import VersionThreeDocument
from sidekick_usages.persistence.schema.account import decode_version_three


class AccountIndexReader:
    """Read saved-account metadata without credential composition."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("Account authority path must be absolute.")
        self.path = path
        self._filesystem = PrivateFileReader(path)

    def load(self) -> tuple[SavedAccount, ...]:
        """Decode the current no-secret account index exactly once."""
        observed = self.observe()
        return () if observed is None else observed.accounts

    def observe(self) -> VersionThreeDocument | None:
        """Passively decode the account authority or prove its absence."""
        observed = self._filesystem.read_opaque_private()
        if observed is None:
            return None
        return decode_version_three(observed.data)

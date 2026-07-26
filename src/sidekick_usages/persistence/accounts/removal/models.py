"""Strict non-secret account-removal models."""

from dataclasses import dataclass
from enum import StrEnum

from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import InvalidSchemaError
from sidekick_usages.persistence.limits import MAX_ACCOUNTS
from sidekick_usages.persistence.types.artifact import Sha256Digest


class AccountRemovalPhase(StrEnum):
    """Durable externally meaningful account-removal boundaries."""

    PREPARED = "prepared"
    PROFILE_RETIRED = "profile_retired"
    METADATA_REMOVED = "metadata_removed"
    FINALIZING = "finalizing"

    @property
    def metadata_removed(self) -> bool:
        """Return whether account metadata was durably removed."""
        return self in {
            AccountRemovalPhase.METADATA_REMOVED,
            AccountRemovalPhase.FINALIZING,
        }

    @property
    def profile_retired(self) -> bool:
        """Return whether provider-profile cleanup was proven."""
        return self in {
            AccountRemovalPhase.PROFILE_RETIRED,
            AccountRemovalPhase.FINALIZING,
        }


@dataclass(frozen=True, slots=True)
class AccountRemovalRecord:
    """One stable-ID removal intent without account or credential metadata."""

    account_id: SidekickAccountId
    provider_id: ProviderId
    expected_account_digest: Sha256Digest | None
    phase: AccountRemovalPhase

    def __post_init__(self) -> None:
        """Require saved-account proof unless metadata was already absent."""
        if (
            self.expected_account_digest is None
            and not self.phase.metadata_removed
        ):
            raise InvalidSchemaError


@dataclass(frozen=True, slots=True)
class AccountRemovalDocument:
    """Bounded removal records ordered by stable account ID."""

    records: tuple[AccountRemovalRecord, ...] = ()

    def __post_init__(self) -> None:
        """Reject duplicate stable IDs and normalize record order."""
        if len(self.records) > MAX_ACCOUNTS:
            raise InvalidSchemaError
        account_ids = {record.account_id for record in self.records}
        if len(account_ids) != len(self.records):
            raise InvalidSchemaError
        object.__setattr__(
            self,
            "records",
            tuple(
                sorted(
                    self.records,
                    key=lambda record: str(record.account_id),
                )
            ),
        )

    def get(
        self,
        account_id: SidekickAccountId,
    ) -> AccountRemovalRecord | None:
        """Return one stable-ID removal record when present."""
        return next(
            (
                record
                for record in self.records
                if record.account_id == account_id
            ),
            None,
        )

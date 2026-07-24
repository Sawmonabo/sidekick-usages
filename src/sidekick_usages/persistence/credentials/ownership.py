"""Credential authority ownership conflict detection."""

from dataclasses import dataclass
from typing import Literal, Protocol

from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.persistence.errors import (
    DuplicateCredentialOwnershipError,
)

type CredentialField = Literal["access_token", "refresh_token"]


class CredentialOwner(Protocol):
    """Fields required to prove provider credential ownership."""

    label: AccountLabel
    provider_id: ProviderId
    access_token: str
    refresh_token: str | None


@dataclass(frozen=True, slots=True)
class CredentialOwnershipConflict:
    """One secret-free exact provider credential ownership conflict."""

    labels: tuple[str, ...]
    provider_id: ProviderId
    credential_field: CredentialField


def _field_conflicts(
    records: tuple[CredentialOwner, ...],
    field_name: CredentialField,
) -> tuple[CredentialOwnershipConflict, ...]:
    handled: set[int] = set()
    conflicts: list[CredentialOwnershipConflict] = []
    for index, record in enumerate(records):
        value = getattr(record, field_name)
        if index in handled or value is None:
            continue
        matching = tuple(
            candidate_index
            for candidate_index in range(index + 1, len(records))
            if records[candidate_index].provider_id is record.provider_id
            and getattr(records[candidate_index], field_name) == value
        )
        if not matching:
            continue
        handled.update(matching)
        labels = (
            str(record.label),
            *(str(records[item].label) for item in matching),
        )
        conflicts.append(
            CredentialOwnershipConflict(
                labels,
                record.provider_id,
                field_name,
            )
        )
    return tuple(conflicts)


def credential_ownership_conflicts(
    records: tuple[CredentialOwner, ...],
) -> tuple[CredentialOwnershipConflict, ...]:
    """Return exact conflicts without deriving credential material."""
    return (
        *_field_conflicts(records, "access_token"),
        *_field_conflicts(records, "refresh_token"),
    )


def reject_duplicate_credential_ownership(
    records: tuple[CredentialOwner, ...],
) -> None:
    """Reject the first deterministic duplicate ownership conflict."""
    conflicts = credential_ownership_conflicts(records)
    if not conflicts:
        return
    conflict = conflicts[0]
    raise DuplicateCredentialOwnershipError(
        conflict.labels,
        provider_id=conflict.provider_id.value,
        credential_field=conflict.credential_field,
    )

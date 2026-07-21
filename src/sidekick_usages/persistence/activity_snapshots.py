"""Strict durable snapshots for authoritative account token activity."""

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
)

from sidekick_usages.core.models import (
    Account,
    AccountTokenActivitySnapshot,
    TokenActivitySummary,
)
from sidekick_usages.core.time import as_utc
from sidekick_usages.core.types import ProviderId, TokenActivityScope
from sidekick_usages.errors import UsageError
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    Sha256Digest,
    sha256_digest,
)
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.serialization import JsonDecodeError, decode_json_value

_MAX_RECORDS = 4_096
_MAX_TOKEN_COUNT = 9_223_372_036_854_775_807


class ActivitySnapshotFailureKind(StrEnum):
    """Closed failures from the token-activity snapshot boundary."""

    READ = "read"
    MALFORMED = "malformed"
    WRITE = "write"
    CONFLICT = "conflict"


class ActivitySnapshotError(UsageError):
    """A token-activity snapshot could not be trusted or persisted."""

    def __init__(self, kind: ActivitySnapshotFailureKind) -> None:
        self.kind = kind
        message = {
            ActivitySnapshotFailureKind.READ: (
                "Saved token activity cannot be read safely."
            ),
            ActivitySnapshotFailureKind.MALFORMED: (
                "Saved token activity is malformed."
            ),
            ActivitySnapshotFailureKind.WRITE: (
                "Fresh token activity could not be saved durably."
            ),
            ActivitySnapshotFailureKind.CONFLICT: (
                "Saved token activity changed concurrently."
            ),
        }[kind]
        super().__init__(message)


def _digest(value: str) -> str:
    Sha256Digest(value)
    return value


def _date_text(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError from None
    if parsed.isoformat() != value:
        raise ValueError
    return value


def _timestamp_text(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        canonical = _timestamp(parsed)
    except ValueError:
        raise ValueError from None
    if canonical != value:
        raise ValueError
    return value


def _records(
    value: dict[str, _ActivitySnapshotRecord],
) -> dict[str, _ActivitySnapshotRecord]:
    if len(value) > _MAX_RECORDS:
        raise ValueError
    return value


type _DigestText = Annotated[str, AfterValidator(_digest)]
type _DateText = Annotated[str, AfterValidator(_date_text)]
type _TimestampText = Annotated[str, AfterValidator(_timestamp_text)]


class _ActivitySnapshotRecord(BaseModel):
    """Private strict persisted snapshot record."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    provider_id: Literal["codex"]
    total_tokens: int = Field(ge=0, le=_MAX_TOKEN_COUNT)
    since: _DateText | None
    fetched_at: _TimestampText


type _SnapshotRecords = Annotated[
    dict[_DigestText, _ActivitySnapshotRecord],
    AfterValidator(_records),
]


class _ActivitySnapshotDocument(BaseModel):
    """Private strict versioned snapshot document."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    schema_version: Literal[1]
    accounts: _SnapshotRecords


_DOCUMENT_ADAPTER = TypeAdapter(_ActivitySnapshotDocument)


def _timestamp(value: datetime) -> str:
    utc_value = as_utc(value)
    return utc_value.isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _encode(document: _ActivitySnapshotDocument) -> bytes:
    root = document.model_dump(mode="python")
    return (
        json.dumps(
            root,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _decode(payload: bytes) -> _ActivitySnapshotDocument:
    try:
        root = decode_json_value(payload)
        document = _DOCUMENT_ADAPTER.validate_python(root, strict=True)
    except JsonDecodeError, ValidationError:
        raise ActivitySnapshotError(
            ActivitySnapshotFailureKind.MALFORMED
        ) from None
    if _encode(document) != payload:
        raise ActivitySnapshotError(ActivitySnapshotFailureKind.MALFORMED)
    return document


def _identity_key(provider_id: ProviderId, account_id: str) -> Sha256Digest:
    try:
        value = f"{provider_id.value}\0{account_id}".encode()
    except UnicodeEncodeError:
        raise ValueError(
            "Provider account identity must be valid UTF-8."
        ) from None
    return sha256_digest(value)


def _record(snapshot: AccountTokenActivitySnapshot) -> _ActivitySnapshotRecord:
    if snapshot.provider_id is not ProviderId.CODEX:
        raise ValueError("Only Codex exposes account activity snapshots.")
    return _ActivitySnapshotRecord(
        provider_id=ProviderId.CODEX.value,
        total_tokens=snapshot.summary.total_tokens,
        since=(
            None
            if snapshot.summary.since is None
            else snapshot.summary.since.isoformat()
        ),
        fetched_at=_timestamp(snapshot.fetched_at),
    )


def _snapshot(
    provider_account_id: str,
    record: _ActivitySnapshotRecord,
) -> AccountTokenActivitySnapshot:
    return AccountTokenActivitySnapshot(
        provider_id=ProviderId(record.provider_id),
        provider_account_id=provider_account_id,
        summary=TokenActivitySummary(
            total_tokens=record.total_tokens,
            scope=TokenActivityScope.ACCOUNT,
            since=(
                None
                if record.since is None
                else date.fromisoformat(record.since)
            ),
        ),
        fetched_at=datetime.fromisoformat(
            record.fetched_at.replace("Z", "+00:00")
        ).astimezone(UTC),
    )


def _merge(
    current: AccountTokenActivitySnapshot | None,
    incoming: AccountTokenActivitySnapshot,
) -> AccountTokenActivitySnapshot:
    if current is None:
        return incoming
    if incoming.fetched_at < current.fetched_at:
        return current
    if incoming.fetched_at == current.fetched_at:
        if incoming == current:
            return current
        raise ActivitySnapshotError(ActivitySnapshotFailureKind.CONFLICT)
    summary = incoming.summary
    if (
        summary.since is None
        and summary.total_tokens >= current.summary.total_tokens
    ):
        summary = replace(summary, since=current.summary.since)
    return replace(incoming, summary=summary)


class ActivitySnapshotStore:
    """Persist last successful account activity under stable identity."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise ValueError("Activity snapshot path must be absolute.")
        self.path = path
        self._filesystem = PersistenceFilesystem(path)
        self._lock = PersistenceLock(self._filesystem)

    @staticmethod
    def _account_id(account: Account) -> str | None:
        if account.provider_id is not ProviderId.CODEX:
            return None
        return account.provider_account_id

    def load(self, account: Account) -> AccountTokenActivitySnapshot | None:
        """Load one exact account snapshot without mutation."""
        if (account_id := self._account_id(account)) is None:
            return None
        try:
            observed = self._filesystem.read_opaque_private()
        except PersistenceError:
            raise ActivitySnapshotError(
                ActivitySnapshotFailureKind.READ
            ) from None
        if observed is None:
            return None
        document = _decode(observed.data)
        record = document.accounts.get(
            str(_identity_key(account.provider_id, account_id))
        )
        return None if record is None else _snapshot(account_id, record)

    def save(
        self,
        snapshot: AccountTokenActivitySnapshot,
    ) -> AccountTokenActivitySnapshot:
        """Merge and durably commit one authoritative account snapshot."""
        key = str(
            _identity_key(
                snapshot.provider_id,
                snapshot.provider_account_id,
            )
        )
        try:
            with self._lock.hold():
                observed = self._filesystem.read_opaque_private()
                if observed is None:
                    document = _ActivitySnapshotDocument(
                        schema_version=1,
                        accounts={},
                    )
                    expected = AuthorityExpectation.ABSENT
                else:
                    document = _decode(observed.data)
                    expected = observed.fingerprint
                current_record = document.accounts.get(key)
                current = (
                    None
                    if current_record is None
                    else _snapshot(
                        snapshot.provider_account_id,
                        current_record,
                    )
                )
                effective = _merge(current, snapshot)
                accounts: Mapping[str, _ActivitySnapshotRecord] = {
                    **document.accounts,
                    key: _record(effective),
                }
                updated = _ActivitySnapshotDocument(
                    schema_version=1,
                    accounts=dict(accounts),
                )
                payload = _encode(updated)
                if observed is not None and observed.data == payload:
                    return effective
                self._filesystem.commit_opaque_private(
                    payload,
                    expected_source=expected,
                )
                return effective
        except ActivitySnapshotError:
            raise
        except PersistenceError:
            raise ActivitySnapshotError(
                ActivitySnapshotFailureKind.WRITE
            ) from None


__all__ = [
    "ActivitySnapshotError",
    "ActivitySnapshotFailureKind",
    "ActivitySnapshotStore",
]

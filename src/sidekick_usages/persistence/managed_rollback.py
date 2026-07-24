"""Rollback preflight for managed provider-owned account authorities."""

from pathlib import Path

from sidekick_usages.persistence.account_schema_v3 import (
    VersionThreeDocument,
    has_managed_authority,
)
from sidekick_usages.persistence.artifacts import (
    FileFingerprint,
    FileSnapshot,
    sha256_digest,
)
from sidekick_usages.persistence.credential_authorities import (
    CredentialAuthorityRepository,
    referenced_legacy_authorities,
)
from sidekick_usages.persistence.errors import (
    InvalidSchemaError,
    ManagedRollbackCompatibilityError,
)


def require_v060_compatible(document: VersionThreeDocument) -> None:
    """Reject rollback before mutation once provider-managed state exists."""
    if has_managed_authority(document):
        raise ManagedRollbackCompatibilityError from None


def authority_bundle_paths(
    document: VersionThreeDocument,
    repository: CredentialAuthorityRepository,
) -> tuple[Path, ...]:
    """Return every referenced protected authority path in stable order."""
    paths = (
        repository.bundle_path(account.account_id, authority_id)
        for account in document.accounts
        for authority_id in referenced_legacy_authorities(account)
    )
    return tuple(sorted(paths, key=lambda path: path.as_posix()))


def guarded_legacy_source(
    source: FileSnapshot,
    document: VersionThreeDocument,
    repository: CredentialAuthorityRepository,
) -> FileSnapshot:
    """Bind a v3 index and every referenced authority into one safe guard."""
    require_v060_compatible(document)
    payload = bytearray(b"sidekick-usages:managed-rollback-source:v1")
    _append_frame(payload, str(source.fingerprint.digest).encode("ascii"))
    for account in document.accounts:
        for authority_id in referenced_legacy_authorities(account):
            protected = repository.read_payload(
                account.account_id,
                authority_id,
            )
            if protected is None:
                raise InvalidSchemaError
            decoded = repository.read(account.account_id, authority_id)
            if (
                decoded is None
                or decoded.provider_id is not account.provider_id
            ):
                raise InvalidSchemaError
            _append_frame(payload, str(account.account_id).encode("ascii"))
            _append_frame(payload, str(authority_id).encode("ascii"))
            _append_frame(
                payload,
                str(sha256_digest(protected)).encode("ascii"),
            )
    data = bytes(payload)
    return FileSnapshot(
        FileFingerprint(
            source.fingerprint.identity,
            sha256_digest(data),
            len(data),
        ),
        source.link_count,
        data,
    )


def _append_frame(target: bytearray, value: bytes) -> None:
    target.extend(len(value).to_bytes(8, "big"))
    target.extend(value)


__all__ = [
    "authority_bundle_paths",
    "guarded_legacy_source",
    "require_v060_compatible",
]

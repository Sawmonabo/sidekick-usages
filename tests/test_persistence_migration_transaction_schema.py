"""Migration credential journal and portable-path contract tests."""

import json

import pytest
from pydantic import ValidationError

from sidekick_usages.persistence.artifacts import (
    AuthorityGeneration,
    sha256_digest,
)
from sidekick_usages.persistence.credential_transaction_schema import (
    AbsentAuthority,
    CredentialSourceGuardRecord,
    CredentialTransactionJournal,
    MigrationCredentialTransactionFile,
    MigrationCredentialTransactionJournal,
    decode_credential_journal,
    encode_credential_journal,
)
from sidekick_usages.persistence.errors import InterruptedArtifactError
from sidekick_usages.persistence.private_bundle_paths import (
    require_portable_unique_private_bundle_paths,
)

_NEW_AUTH = b"test-only-new-private-auth"


def test_journal_versions_are_strict_and_generation_coherent() -> None:
    digest = str(sha256_digest(b""))
    version_one = CredentialTransactionJournal(
        journal_version=1,
        base_authority=AbsentAuthority(kind="absent"),
        source_guard=None,
        target_authority_sha256=digest,
        target_authority_size=0,
        target_bundles=(),
        base_present_bundles=(),
        files=(),
        displaced_bundles=(),
    )
    decoded_v1 = decode_credential_journal(
        encode_credential_journal(version_one)
    )
    assert type(decoded_v1) is CredentialTransactionJournal

    v1_with_v2_field = version_one.model_dump(mode="json")
    v1_with_v2_field["target_generation"] = "v1"
    with pytest.raises(InterruptedArtifactError):
        decode_credential_journal(
            json.dumps(v1_with_v2_field, sort_keys=True).encode()
        )

    source_guard = CredentialSourceGuardRecord(
        path_sha256=digest,
        authority=AbsentAuthority(kind="absent"),
    )
    version_two = MigrationCredentialTransactionJournal(
        journal_version=2,
        base_authority=AbsentAuthority(kind="absent"),
        base_generation=None,
        source_guard=source_guard,
        target_generation=AuthorityGeneration.VERSION_ONE,
        target_authority_sha256=digest,
        target_authority_size=0,
        target_bundles=(),
        base_present_bundles=(),
        files=(),
        displaced_bundles=(),
    )
    decoded_v2 = decode_credential_journal(
        encode_credential_journal(version_two)
    )
    assert type(decoded_v2) is MigrationCredentialTransactionJournal
    assert decoded_v2.target_generation is AuthorityGeneration.VERSION_ONE

    with pytest.raises(ValidationError):
        MigrationCredentialTransactionJournal(
            journal_version=2,
            base_authority=AbsentAuthority(kind="absent"),
            base_generation=AuthorityGeneration.VERSION_ONE,
            source_guard=source_guard,
            target_generation=AuthorityGeneration.VERSION_ONE,
            target_authority_sha256=digest,
            target_authority_size=0,
            target_bundles=(),
            base_present_bundles=(),
            files=(),
            displaced_bundles=(),
        )


@pytest.mark.parametrize(
    "bundle_path",
    [
        "/absolute",
        "../traversal",
        "nested//empty",
        "nested\\windows",
        "CON/auth",
        "COM¹/auth",
        "C:drive/auth",
        "nested/trailing.",
        "cafe\u0301/auth",
        "/".join("part" for _index in range(9)),
        "a" * 256,
        "/".join("a" * 128 for _index in range(8)),
    ],
)
def test_version_two_rejects_unsafe_relative_bundle_paths(
    bundle_path: str,
) -> None:
    with pytest.raises(ValidationError):
        MigrationCredentialTransactionFile(
            bundle_path=bundle_path,
            basename="auth.json",
            stage_basename="stage-0000.bin",
            backup_basename=None,
            base_sha256=None,
            target_sha256=str(sha256_digest(_NEW_AUTH)),
        )


@pytest.mark.parametrize(
    "paths",
    [
        ("Team/primary", "team/primary"),
        ("team", "team/primary"),
        ("Team/first", "team/second"),
    ],
)
def test_version_two_rejects_portable_alias_and_ancestor_collisions(
    paths: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError, match="Private bundle paths"):
        require_portable_unique_private_bundle_paths(paths)

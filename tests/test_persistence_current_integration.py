"""Current-generation inventory, runtime, migration, and rollback tests."""

from pathlib import Path

import pytest

from sidekick_usages.persistence.account_store import AccountStoreStateError
from sidekick_usages.persistence.assessment import (
    PersistenceCode,
    assess_persistence,
)
from sidekick_usages.persistence.inventory import OrphanedPrivateCredentials
from sidekick_usages.persistence.schemas import (
    VersionOneDocument,
    decode_generation_zero,
    decode_version_two,
    encode_version_one,
    encode_version_two,
)
from sidekick_usages.persistence.transforms import (
    accounts_to_version_one,
    accounts_to_version_two,
    version_two_to_accounts,
)
from tests.test_persistence_account_store import _store
from tests.test_persistence_coordinator import _account, _service
from tests.test_persistence_inventory import (
    AUTHORITY_PATH,
    PROTOTYPE_PATH,
    FakeFilesystem,
    _inventory,
    _snapshot,
)

_CURRENT_SCHEMA_VERSION = 2


def test_inventory_marks_only_version_two_as_runtime_current() -> None:
    """Valid schema one requires explicit migration; schema two is current."""
    authority = FakeFilesystem(AUTHORITY_PATH)
    prototype = FakeFilesystem(PROTOTYPE_PATH)
    current = encode_version_two(
        accounts_to_version_two((_account("claude-current"),))
    )
    authority.snapshots[AUTHORITY_PATH.name] = _snapshot(current)

    current_observation = _inventory(authority, prototype).inspect(
        OrphanedPrivateCredentials.ABSENT
    )
    current_assessment = assess_persistence(current_observation)

    assert current_observation.authority.kind.value == "version_two"
    assert current_assessment.code is PersistenceCode.CURRENT
    assert current_assessment.schema_version == _CURRENT_SCHEMA_VERSION

    legacy_authority = FakeFilesystem(AUTHORITY_PATH)
    legacy_prototype = FakeFilesystem(PROTOTYPE_PATH)
    legacy = encode_version_one(VersionOneDocument(()))
    legacy_authority.snapshots[AUTHORITY_PATH.name] = _snapshot(legacy)

    legacy_observation = _inventory(
        legacy_authority,
        legacy_prototype,
    ).inspect(OrphanedPrivateCredentials.ABSENT)
    legacy_assessment = assess_persistence(legacy_observation)

    assert legacy_observation.authority.kind.value == "version_one"
    assert legacy_assessment.code is PersistenceCode.MIGRATION_REQUIRED
    assert legacy_assessment.next_command == (
        "sidekick-usages",
        "migrate",
        "accounts",
    )


def test_coordinator_migrates_rolls_back_and_reconstructs_current(
    tmp_path: Path,
) -> None:
    """Atomic lineage spans legacy v1, current v2, and released state."""
    accounts = (_account("claude-current"),)
    legacy = encode_version_one(
        VersionOneDocument(accounts_to_version_one(accounts).accounts)
    )
    current = encode_version_two(accounts_to_version_two(accounts))
    service, authority, _prototype, log, _scheduler, _verifier = _service(
        tmp_path,
        legacy,
    )

    migrated = service.migrate_accounts()

    assert migrated.code is PersistenceCode.CURRENT
    assert authority.snapshot is not None
    assert authority.snapshot.data == current
    assert log[:2] == ["snapshot:v1", "commit:v2"]
    assert service.read_accounts() == accounts

    rollback = service.prepare_rollback()

    assert rollback.code is PersistenceCode.ROLLBACK_PREPARED
    assert authority.snapshot is not None
    decode_generation_zero(authority.snapshot.data)
    assert "snapshot:v2" in log
    assert "commit:v0" in log

    reconstructed = service.migrate_accounts()

    assert reconstructed.code is PersistenceCode.CURRENT
    assert authority.snapshot is not None
    document = decode_version_two(authority.snapshot.data)
    assert version_two_to_accounts(document) == accounts


def test_runtime_store_accepts_only_current_or_absent_state(
    tmp_path: Path,
) -> None:
    """Runtime never loads schema one and every write remains schema two."""
    account = _account("claude-current")
    current = encode_version_two(accounts_to_version_two((account,)))
    current_store, filesystem, _observer = _store(
        tmp_path / "current",
        current,
    )

    current_store.load()
    assert current_store.get("claude-current") == account
    current_store.persist(_account("claude-second"))
    assert filesystem.snapshot is not None
    decode_version_two(filesystem.snapshot.data)

    legacy_store, legacy_filesystem, _observer = _store(
        tmp_path / "legacy",
        encode_version_one(accounts_to_version_one((account,))),
    )
    with pytest.raises(AccountStoreStateError) as exc_info:
        legacy_store.load()
    assert exc_info.value.code is PersistenceCode.MIGRATION_REQUIRED
    assert legacy_filesystem.snapshot is not None
    assert legacy_filesystem.snapshot.data.startswith(
        b'{\n  "schema_version": 1'
    )

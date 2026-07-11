"""End-to-end persistence-location service and composition behavior."""

import base64
import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from rich.console import Console

from sidekick_usages.cli.context import (
    DoctorReady,
    PersistenceContext,
    compose_app_context,
    compose_doctor_context,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import AccountLabel, ExitCode
from sidekick_usages.paths import (
    AccountLocations,
    ApplicationPaths,
    PrivateCodexLocations,
)
from sidekick_usages.persistence.artifacts import (
    AuthorityExpectation,
    AuthorityGeneration,
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.errors import PersistenceCode
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.migrations import PersistenceMigrationService
from sidekick_usages.persistence.migrations.errors import (
    LocationMigrationStateError,
)
from sidekick_usages.persistence.migrations.location import (
    CandidateBlockedSelection,
    CanonicalSelection,
    CompatibilitySelection,
    ConflictSelection,
    EmptySelection,
    EquivalentSelection,
    LocationMigrationAssessment,
    LocationRole,
    PartialSelection,
    PrototypeSelection,
    ReadyLocationSelection,
)
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schemas import encode_version_one
from sidekick_usages.persistence.transforms import accounts_to_version_one
from sidekick_usages.persistence.v060 import ReleasedV060Verifier
from sidekick_usages.providers.codex.auth import (
    CODEX_AUTH_FILE,
    CODEX_CONFIG_FILE,
    CODEX_FILE_AUTH_CONFIG,
)
from sidekick_usages.providers.codex.auth_migration import (
    CodexPrivateAuthMigrator,
)
from sidekick_usages.scheduler_quiescence import (
    SchedulerBackendId,
    SchedulerBackendObservation,
    SchedulerBackendState,
    SchedulerQuiescenceAssessment,
)
from tests.test_support import CliHarness

MIGRATE_ACCOUNTS = ("sidekick-usages", "migrate", "accounts")
MIGRATE_LOCATIONS = ("sidekick-usages", "migrate", "locations")
PROTOTYPE = b'{"imported":{"token":"test-only","plan":"max"}}'
QUIET = SchedulerQuiescenceAssessment(
    (
        SchedulerBackendObservation(
            SchedulerBackendId.SYSTEMD,
            SchedulerBackendState.ABSENT,
            "Sidekick scheduler is absent.",
        ),
    )
)


def _paths(root: Path) -> ApplicationPaths:
    """Return distinct compatibility, canonical, and prototype paths."""
    compatibility = root / "compatibility"
    canonical = root / "canonical"
    return ApplicationPaths(
        accounts=AccountLocations(
            canonical=canonical / "accounts.json",
            existing_sidekick=compatibility / "accounts.json",
            prototype_cc_usage=root / "prototype" / "accounts.json",
        ),
        private_codex=PrivateCodexLocations(
            canonical=canonical / "codex",
            existing_sidekick=compatibility / "codex",
        ),
    )


def _service(
    paths: ApplicationPaths,
    *,
    filesystem_factory: Callable[[Path], PersistenceFilesystem] = (
        PersistenceFilesystem
    ),
) -> PersistenceMigrationService:
    """Compose the real location service around isolated native storage."""
    return PersistenceMigrationService(
        paths,
        scheduler_assessor=lambda: QUIET,
        private_auth_migrator=CodexPrivateAuthMigrator(),
        released_v060_verifier=ReleasedV060Verifier(),
        filesystem_factory=filesystem_factory,
    )


def _claude_account(label: str, token: str = "test-only-token") -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=ClaudeCredentials(access_token=token),
    )


def _access_token(account_id: str) -> str:
    def encode(value: dict[str, object]) -> str:
        payload = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    header = encode({"alg": "none"})
    claims = encode(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
            }
        }
    )
    return f"{header}.{claims}.test-only-signature"


def _codex_account(label: str, home: Path, account_id: str) -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=CodexCredentials(
            access_token=_access_token(account_id),
            refresh_token=f"test-only-refresh-{account_id}",
            account_id=account_id,
            auth_home=str(home),
        ),
    )


def _auth_bundle(account_id: str, marker: str) -> dict[str, bytes]:
    return {
        CODEX_AUTH_FILE: json.dumps(
            {
                "test_marker": marker,
                "tokens": {
                    "access_token": _access_token(account_id),
                    "account_id": account_id,
                },
            },
            sort_keys=True,
        ).encode(),
        CODEX_CONFIG_FILE: f"{CODEX_FILE_AUTH_CONFIG}\n".encode(),
    }


def _authority_payload(accounts: tuple[Account, ...]) -> bytes:
    return encode_version_one(accounts_to_version_one(accounts))


def _seed_authority(path: Path, accounts: tuple[Account, ...]) -> bytes:
    payload = _authority_payload(accounts)
    filesystem = PersistenceFilesystem(path)
    filesystem.repair_parent_permissions()
    with PersistenceLock(filesystem).hold() as transaction:
        transaction.commit_authority(
            AuthorityGeneration.VERSION_ONE,
            payload,
            AuthorityExpectation.ABSENT,
        )
    return payload


def _replace_authority(path: Path, accounts: tuple[Account, ...]) -> None:
    filesystem = PersistenceFilesystem(path)
    source = filesystem.read_authority()
    assert source is not None
    with PersistenceLock(filesystem).hold() as transaction:
        transaction.commit_authority(
            AuthorityGeneration.VERSION_ONE,
            _authority_payload(accounts),
            source.fingerprint,
        )


def _seed_opaque(path: Path, payload: bytes) -> None:
    PersistenceFilesystem(path).commit_opaque_private(
        payload,
        expected_source=AuthorityExpectation.ABSENT,
    )


def _seed_bundle(
    paths: ApplicationPaths,
    role: LocationRole,
    home: Path,
    files: dict[str, bytes],
) -> None:
    root = (
        paths.private_codex.canonical
        if role is LocationRole.CANONICAL
        else paths.private_codex.existing_sidekick
    )
    account_path = (
        paths.accounts.canonical
        if role is LocationRole.CANONICAL
        else paths.accounts.existing_sidekick
    )
    tree = PrivateCredentialTree(
        root,
        account_path=account_path,
        existing_root=paths.private_codex.existing_sidekick,
    )
    tree.write_bundle(
        home,
        files,
        expected_bundle_present=False,
        expected_files=dict.fromkeys(files),
    )


def _arrange_matrix_case(case: str, paths: ApplicationPaths) -> None:
    account = _claude_account("shared")
    if case == "prototype-only":
        _seed_opaque(paths.accounts.prototype_cc_usage, PROTOTYPE)
    elif case == "compatibility-only":
        _seed_authority(paths.accounts.existing_sidekick, (account,))
    elif case in {"canonical-only", "stale-malformed-prototype"}:
        _seed_authority(paths.accounts.canonical, (account,))
        if case == "stale-malformed-prototype":
            _seed_opaque(paths.accounts.prototype_cc_usage, b"{")
    elif case == "equivalent":
        _seed_authority(paths.accounts.existing_sidekick, (account,))
        _seed_authority(paths.accounts.canonical, (account,))
    elif case == "account-conflict":
        _seed_authority(
            paths.accounts.existing_sidekick,
            (_claude_account("shared", "test-only-old"),),
        )
        _seed_authority(
            paths.accounts.canonical,
            (_claude_account("shared", "test-only-new"),),
        )
    elif case == "private-auth-partial":
        account_id = "acct_shared"
        compatibility_home = (
            paths.private_codex.existing_sidekick / "teams" / "shared"
        )
        canonical_home = paths.private_codex.canonical / "teams" / "shared"
        _seed_authority(
            paths.accounts.existing_sidekick,
            (_codex_account("shared", compatibility_home, account_id),),
        )
        _seed_authority(
            paths.accounts.canonical,
            (_codex_account("shared", canonical_home, account_id),),
        )
        _seed_bundle(
            paths,
            LocationRole.COMPATIBILITY,
            compatibility_home,
            _auth_bundle(account_id, "compatibility"),
        )
        _seed_bundle(
            paths,
            LocationRole.CANONICAL,
            canonical_home,
            _auth_bundle(account_id, "canonical"),
        )
    elif case == "malformed-authority":
        _seed_opaque(paths.accounts.existing_sidekick, b"{")


@pytest.mark.parametrize(
    (
        "case",
        "selection_type",
        "roles",
        "codes",
        "source",
        "next_command",
    ),
    [
        pytest.param(
            "empty",
            EmptySelection,
            (),
            (),
            "canonical",
            None,
            id="empty",
        ),
        pytest.param(
            "prototype-only",
            PrototypeSelection,
            (LocationRole.PROTOTYPE,),
            (PersistenceCode.PROTOTYPE_IMPORT_REQUIRED,),
            "prototype",
            MIGRATE_ACCOUNTS,
            id="prototype-only",
        ),
        pytest.param(
            "compatibility-only",
            CompatibilitySelection,
            (LocationRole.COMPATIBILITY,),
            (PersistenceCode.CURRENT,),
            "compatibility",
            MIGRATE_LOCATIONS,
            id="compatibility-only",
        ),
        pytest.param(
            "canonical-only",
            CanonicalSelection,
            (LocationRole.CANONICAL,),
            (PersistenceCode.CURRENT,),
            "canonical",
            None,
            id="canonical-only",
        ),
        pytest.param(
            "equivalent",
            EquivalentSelection,
            (LocationRole.COMPATIBILITY, LocationRole.CANONICAL),
            (PersistenceCode.CURRENT, PersistenceCode.CURRENT),
            "canonical",
            None,
            id="equivalent",
        ),
        pytest.param(
            "account-conflict",
            ConflictSelection,
            (LocationRole.COMPATIBILITY, LocationRole.CANONICAL),
            (PersistenceCode.CURRENT, PersistenceCode.CURRENT),
            "compatibility",
            None,
            id="account-conflict",
        ),
        pytest.param(
            "private-auth-partial",
            PartialSelection,
            (LocationRole.COMPATIBILITY, LocationRole.CANONICAL),
            (PersistenceCode.CURRENT, PersistenceCode.CURRENT),
            "compatibility",
            None,
            id="private-auth-partial",
        ),
        pytest.param(
            "malformed-authority",
            CandidateBlockedSelection,
            (LocationRole.COMPATIBILITY,),
            (PersistenceCode.MALFORMED_JSON,),
            "compatibility",
            None,
            id="malformed-authority",
        ),
        pytest.param(
            "stale-malformed-prototype",
            CanonicalSelection,
            (LocationRole.CANONICAL,),
            (PersistenceCode.CURRENT,),
            "canonical",
            None,
            id="stale-malformed-prototype",
        ),
    ],
)
def test_real_location_matrix_selects_one_exact_runtime_state(
    tmp_path: Path,
    case: str,
    selection_type: type[object],
    roles: tuple[LocationRole, ...],
    codes: tuple[PersistenceCode, ...],
    source: str,
    next_command: tuple[str, ...] | None,
) -> None:
    """Real filesystem evidence closes every location-selection branch."""
    paths = _paths(tmp_path / case)
    _arrange_matrix_case(case, paths)

    assessment = _service(paths).assess_locations()
    expected_source = {
        "canonical": paths.accounts.canonical,
        "compatibility": paths.accounts.existing_sidekick,
        "prototype": paths.accounts.prototype_cc_usage,
    }[source]

    assert type(assessment.selection) is selection_type
    assert (
        tuple(candidate.role for candidate in assessment.candidates) == roles
    )
    assert (
        tuple(candidate.assessment.code for candidate in assessment.candidates)
        == codes
    )
    assert assessment.source == expected_source
    assert assessment.destination == paths.accounts.canonical
    assert assessment.next_command == next_command


def test_compatibility_runtime_never_relocates_implicitly(
    tmp_path: Path,
) -> None:
    """Normal and doctor composition read compatibility without writing."""
    paths = _paths(tmp_path)
    original = _seed_authority(
        paths.accounts.existing_sidekick,
        (_claude_account("compatibility"),),
    )

    application = compose_app_context(
        paths=paths,
        providers={},
        heartbeat_providers={},
    )
    doctor = compose_doctor_context(
        paths=paths,
        providers={},
        heartbeat_providers={},
    )
    try:
        assert [
            str(account.label) for account in application.value.accounts
        ] == ["compatibility"]
        assert isinstance(doctor.value.state, DoctorReady)
        assert isinstance(
            doctor.value.state.assessment.selection,
            CompatibilitySelection,
        )
        assert doctor.value.state.assessment.source == (
            paths.accounts.existing_sidekick
        )
    finally:
        doctor.close()
        application.close()

    assert paths.accounts.existing_sidekick.read_bytes() == original
    assert not paths.accounts.canonical.exists()
    assert not paths.private_codex.canonical.exists()


def test_empty_runtime_first_persist_creates_only_canonical_state(
    tmp_path: Path,
) -> None:
    """A first authorized write is native state, not a relocation claim."""
    paths = _paths(tmp_path)
    application = compose_app_context(
        paths=paths,
        providers={},
        heartbeat_providers={},
    )
    try:
        application.value.accounts.persist(_claude_account("first"))
    finally:
        application.close()

    assert paths.accounts.canonical.is_file()
    assert not paths.accounts.existing_sidekick.exists()
    assessment = _service(paths).assess_locations()
    assert isinstance(assessment.selection, CanonicalSelection)
    assert assessment.source == paths.accounts.canonical


def test_normal_composition_rejects_a_post_load_location_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source replacement after store load cannot become runtime state."""
    paths = _paths(tmp_path)
    _seed_authority(
        paths.accounts.existing_sidekick,
        (_claude_account("before"),),
    )
    original = PersistenceMigrationService.require_location_unchanged
    raced = False

    def replace_before_reassessment(
        service: PersistenceMigrationService,
        expected: LocationMigrationAssessment[ReadyLocationSelection],
    ) -> None:
        nonlocal raced
        if not raced:
            raced = True
            _replace_authority(
                paths.accounts.existing_sidekick,
                (_claude_account("after"),),
            )
        original(service, expected)

    monkeypatch.setattr(
        PersistenceMigrationService,
        "require_location_unchanged",
        replace_before_reassessment,
    )

    with pytest.raises(LocationMigrationStateError) as race:
        compose_app_context(
            paths=paths,
            providers={},
            heartbeat_providers={},
        )

    assert raced
    assert isinstance(
        race.value.assessment.selection,
        CompatibilitySelection,
    )
    assert race.value.assessment.source == paths.accounts.existing_sidekick
    assert not paths.accounts.canonical.exists()


class _OrderingFilesystem(PersistenceFilesystem):
    """Observe only whether private state precedes canonical authority."""

    def __init__(
        self,
        path: Path,
        canonical_account: Path,
        canonical_bundle: Path,
        authority_checks: list[bool],
    ) -> None:
        super().__init__(path)
        self._canonical_account = canonical_account
        self._canonical_bundle = canonical_bundle
        self._authority_checks = authority_checks

    def _commit_authority(
        self,
        generation: AuthorityGeneration,
        payload: bytes,
        expected_source: ExpectedAuthority,
    ) -> FileSnapshot:
        if self.authority_path == self._canonical_account:
            self._authority_checks.append(
                (self._canonical_bundle / CODEX_AUTH_FILE).is_file()
                and (self._canonical_bundle / CODEX_CONFIG_FILE).is_file()
            )
        return super()._commit_authority(
            generation,
            payload,
            expected_source,
        )


def test_migrate_locations_requires_intent_and_commits_private_auth_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sole relocation command preserves every old and external byte."""
    paths = _paths(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "provider-native"))
    account_id = "acct_owned"
    compatibility_home = (
        paths.private_codex.existing_sidekick / "teams" / "nested" / "owned"
    )
    canonical_home = (
        paths.private_codex.canonical / "teams" / "nested" / "owned"
    )
    external_home = tmp_path / "external" / "codex"
    external_file = external_home / "external.txt"
    external_home.mkdir(parents=True)
    external_file.write_bytes(b"test-only-external-bytes")
    accounts = (
        _codex_account("owned", compatibility_home, account_id),
        _codex_account("external", external_home, "acct_external"),
    )
    compatibility_bytes = _seed_authority(
        paths.accounts.existing_sidekick,
        accounts,
    )
    compatibility_bundle = _auth_bundle(account_id, "compatibility")
    _seed_bundle(
        paths,
        LocationRole.COMPATIBILITY,
        compatibility_home,
        compatibility_bundle,
    )
    _seed_opaque(paths.accounts.prototype_cc_usage, PROTOTYPE)
    prototype_bytes = paths.accounts.prototype_cc_usage.read_bytes()
    external_bytes = external_file.read_bytes()
    authority_checks: list[bool] = []

    def filesystem_factory(path: Path) -> PersistenceFilesystem:
        return _OrderingFilesystem(
            path,
            paths.accounts.canonical,
            canonical_home,
            authority_checks,
        )

    service = _service(paths, filesystem_factory=filesystem_factory)
    stdout = io.StringIO()
    harness = CliHarness(
        console=Console(file=stdout, width=200, force_terminal=False),
        err_console=Console(
            file=io.StringIO(),
            width=200,
            force_terminal=False,
        ),
        persistence=PersistenceContext(service),
    )

    refused = harness.invoke(["migrate", "locations"], input_text="n\n")

    assert refused.exit_code == ExitCode.MANUAL_ACTION
    assert not paths.accounts.canonical.exists()
    assert not canonical_home.exists()
    assert authority_checks == []

    migrated = harness.invoke(["migrate", "locations", "--yes"])

    assert migrated.exit_code == ExitCode.SUCCESS
    assert authority_checks == [True]
    assessment = service.assess_locations()
    assert isinstance(
        assessment.selection,
        (CanonicalSelection, EquivalentSelection),
    )
    migrated_accounts = {
        str(account.label): account for account in service.read_accounts()
    }
    assert migrated_accounts["owned"].codex_home == str(canonical_home)
    assert migrated_accounts["external"].codex_home == str(external_home)
    canonical_tree = PrivateCredentialTree(
        paths.private_codex.canonical,
        account_path=paths.accounts.canonical,
        existing_root=paths.private_codex.existing_sidekick,
    )
    observed_bundle = canonical_tree.read_relative_bundle("teams/nested/owned")
    assert observed_bundle is not None
    assert {
        basename: snapshot.data
        for basename, snapshot in observed_bundle.items()
    } == compatibility_bundle
    assert paths.accounts.existing_sidekick.read_bytes() == (
        compatibility_bytes
    )
    assert {
        basename: (compatibility_home / basename).read_bytes()
        for basename in compatibility_bundle
    } == compatibility_bundle
    assert paths.accounts.prototype_cc_usage.read_bytes() == prototype_bytes
    assert external_file.read_bytes() == external_bytes


def test_native_rollback_preserves_latest_state_for_released_reader(
    tmp_path: Path,
) -> None:
    """Native writes reverse into compatibility with private auth first."""
    paths = _paths(tmp_path)
    original_accounts = (_claude_account("original"),)
    original_compatibility = _seed_authority(
        paths.accounts.existing_sidekick,
        original_accounts,
    )
    _service(paths).migrate_locations()

    account_id = "acct_rollback"
    canonical_home = paths.private_codex.canonical / "teams" / "rollback"
    compatibility_home = (
        paths.private_codex.existing_sidekick / "teams" / "rollback"
    )
    bundle = _auth_bundle(account_id, "latest-canonical")
    _seed_bundle(
        paths,
        LocationRole.CANONICAL,
        canonical_home,
        bundle,
    )
    latest_accounts = (
        *original_accounts,
        _codex_account("rollback", canonical_home, account_id),
    )
    _replace_authority(paths.accounts.canonical, latest_accounts)
    canonical_latest = paths.accounts.canonical.read_bytes()
    assert isinstance(
        _service(paths).assess_locations().selection,
        CanonicalSelection,
    )

    authority_checks: list[bool] = []

    def filesystem_factory(path: Path) -> PersistenceFilesystem:
        return _OrderingFilesystem(
            path,
            paths.accounts.existing_sidekick,
            compatibility_home,
            authority_checks,
        )

    result = _service(
        paths,
        filesystem_factory=filesystem_factory,
    ).prepare_rollback()

    assert result.code is PersistenceCode.ROLLBACK_PREPARED
    assert result.assessment.code is PersistenceCode.ROLLBACK_PREPARED
    assert authority_checks == [True]
    assert paths.accounts.canonical.read_bytes() == canonical_latest
    assert {
        basename: (compatibility_home / basename).read_bytes()
        for basename in bundle
    } == bundle
    compatibility = PersistenceFilesystem(
        paths.accounts.existing_sidekick
    ).read_authority()
    assert compatibility is not None
    ReleasedV060Verifier().verify(
        paths.accounts.existing_sidekick,
        compatibility,
    )
    compatibility_snapshots = tuple(
        paths.accounts.existing_sidekick.parent.glob("accounts.json.v1.*.bak")
    )
    assert any(
        snapshot.read_bytes() == original_compatibility
        for snapshot in compatibility_snapshots
    )
    assert result.artifact_basename is not None
    rollback_snapshot = (
        paths.accounts.existing_sidekick.parent / result.artifact_basename
    )
    assert rollback_snapshot.is_file()

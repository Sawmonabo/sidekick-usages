"""Synthetic managed Codex account and private-home fixtures."""

import json
import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

from sidekick_usages.core.accounts.models import (
    CodexAccountAuthority,
    CodexManagedAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialHealth,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.codex.managed.service import (
    CodexManagedAuthorityCoordinator,
)
from sidekick_usages.paths import ApplicationPaths, managed_codex_home
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.locking import PersistenceLock
from sidekick_usages.persistence.models.account import VersionThreeDocument
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.schema.account import encode_version_three
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from sidekick_usages.providers.codex.app_server.capabilities import (
    probe_codex_capabilities,
)
from sidekick_usages.providers.codex.app_server.executable import (
    discover_codex_executable,
)
from sidekick_usages.providers.codex.auth.storage import (
    CODEX_AUTH_FILE,
    CODEX_CONFIG_FILE,
    CODEX_FILE_AUTH_CONFIG,
)
from tests.fakes.codex.app_server.executable import write_fake_codex
from tests.fakes.codex.app_server.schema import write_codex_schema
from tests.fakes.codex.auth import NEXT_AUTH_FILE, managed_auth
from tests.support.persistence import make_application_paths
from tests.support.time import REFERENCE_TIME, FixedClock

MANAGED_FILE_CONFIG = f"{CODEX_FILE_AUTH_CONFIG}\n".encode()


def managed_saved_account(
    account_id: SidekickAccountId,
    authority_id: AuthorityId,
    label: str,
    provider_identity: str,
    generation: str,
) -> SavedAccount:
    """Build one secret-free managed Codex account."""
    return SavedAccount(
        account_id=account_id,
        label=AccountLabel(label),
        provider_id=ProviderId.CODEX,
        plan="pro",
        authority=CodexAccountAuthority(
            subscription=CodexManagedAuthority(
                authority_id=authority_id,
                provider_identity=ProviderIdentity(provider_identity),
                generation=AuthorityGeneration(generation),
                verified_at=REFERENCE_TIME - timedelta(minutes=5),
                executable_version="0.145.0",
                health=CredentialHealth.HEALTHY,
            )
        ),
        credential_health=CredentialHealth.HEALTHY,
    )


def managed_subscription(account: SavedAccount) -> CodexManagedAuthority:
    """Return the managed subscription from a synthetic Codex account."""
    authority = account.authority
    assert isinstance(authority, CodexAccountAuthority)
    subscription = authority.subscription
    assert isinstance(subscription, CodexManagedAuthority)
    return subscription


def seed_managed_accounts(
    root: Path,
    accounts: tuple[SavedAccount, ...],
    next_authorities: Mapping[SidekickAccountId, bytes],
) -> tuple[ApplicationPaths, AccountStore, PrivateCredentialTree]:
    """Persist managed metadata and independent synthetic Codex homes."""
    paths = make_application_paths(root)
    filesystem = PersistenceFilesystem(paths.accounts)
    filesystem.repair_parent_permissions()
    with PersistenceLock(filesystem).hold() as transaction:
        transaction.commit_authority(
            encode_version_three(VersionThreeDocument(accounts)),
            AuthorityExpectation.ABSENT,
        )
    managed_tree = PrivateCredentialTree(
        paths.private_codex_profiles,
        account_path=paths.accounts,
    )
    for account in accounts:
        authority = managed_subscription(account)
        managed_tree.write_bundle(
            managed_codex_home(paths, account.account_id),
            {
                CODEX_AUTH_FILE: managed_auth(
                    str(authority.provider_identity),
                    str(authority.generation),
                ),
                CODEX_CONFIG_FILE: MANAGED_FILE_CONFIG,
                NEXT_AUTH_FILE: next_authorities[account.account_id],
            },
            expected_bundle_present=False,
            expected_files={},
        )
    credential_tree = PrivateCredentialTree(
        paths.private_credentials,
        account_path=paths.accounts,
    )
    return (
        paths,
        AccountStore(paths.accounts, credential_tree).load(),
        managed_tree,
    )


def managed_coordinator(
    root: Path,
    paths: ApplicationPaths,
    store: AccountStore,
    private: PrivateCredentialTree,
) -> CodexManagedAuthorityCoordinator:
    """Compose one managed coordinator around a release-matched fake."""
    schema_root = root / "schema"
    write_codex_schema(schema_root, external_auth=True)
    write_fake_codex(root, schema_root)
    environment = {
        "HOME": str(root),
        "PATH": os.pathsep.join((str(root), os.environ["PATH"])),
    }
    executable = discover_codex_executable(environment)
    capabilities = probe_codex_capabilities(executable, environment)
    return CodexManagedAuthorityCoordinator(
        paths,
        store,
        private,
        capabilities,
        FixedClock(),
        environment=environment,
    )


def managed_generation(
    private: PrivateCredentialTree,
    account_id: SidekickAccountId,
) -> str:
    """Read the current synthetic provider-owned generation."""
    snapshot = private.read_relative_bundle_file(
        str(account_id),
        CODEX_AUTH_FILE,
    )
    assert snapshot is not None
    generation = json.loads(snapshot.data)["last_refresh"]
    assert isinstance(generation, str)
    return generation

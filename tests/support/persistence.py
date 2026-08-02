"""Isolated persistence fixtures."""

from collections.abc import Iterable
from pathlib import Path

from sidekick_usages.core.models import Account
from sidekick_usages.core.selection.models import FinalizedSelection
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.paths import ApplicationPaths
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.models.selection import SelectedStateDocument
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.private.filesystem import PrivateFilesystem
from sidekick_usages.persistence.schema.selection import encode_selected_state
from sidekick_usages.persistence.types.artifact import AuthorityExpectation


def make_application_paths(root: Path) -> ApplicationPaths:
    """Build isolated Sidekick-owned locations below ``root``."""
    account_file = root / "accounts.json"
    private_root = root / "credentials"
    return ApplicationPaths(
        accounts=account_file,
        private_credentials=private_root,
        private_codex_profiles=private_root / "codex",
        activity_snapshots=root / "token-activity.json",
        usage_snapshots=root / "usage-metrics.json",
        metrics_refresh_status=root / "metrics-refresh.json",
        credential_refresh=root / "credential-refresh",
        private_claude_profiles=private_root / "claude",
        selected_state=root / "selected-accounts.json",
        activation_journals=root / "activation-journals",
        selection_journals=root / "selection-journals",
        durable_operations=root / "operations",
        codex_session_home=root / "sessions" / "codex",
        shell_integration=root / "shell-integration.sh",
        service_state=root / "service-state.json",
        service_setup_acknowledgement=(
            root / "service-setup-acknowledgement.json"
        ),
        service_logs=root / "logs",
        runtime_directory=root / "runtime",
        supervisor_socket=root / "runtime" / "supervisor.sock",
        participant_sockets=root / "runtime" / "participants",
        systemd_user_service=root / "home/systemd/sidekick-usages.service",
        launch_agent=(
            root
            / "home"
            / "LaunchAgents"
            / "com.sidekick-usages.supervisor.plist"
        ),
    )


def make_account_store(
    root: Path,
    accounts: Iterable[Account] = (),
) -> AccountStore:
    """Build a loaded transactional store with a live private observer."""
    store, _private = make_account_store_with_private(root, accounts)
    return store


def make_account_store_with_private(
    root: Path,
    accounts: Iterable[Account] = (),
) -> tuple[AccountStore, PrivateCredentialTree]:
    """Build a store and the exact private tree injected into it."""
    paths = make_application_paths(root)
    PersistenceFilesystem(paths.accounts).repair_parent_permissions()
    private_credentials = PrivateCredentialTree(
        paths.private_credentials,
        account_path=paths.accounts,
    )
    store = AccountStore(paths.accounts, private_credentials).load()
    for account in accounts:
        store.persist(account)
    return store, private_credentials


def remove_saved_account(
    store: AccountStore,
    label: AccountLabel,
) -> None:
    """Remove one exact saved test account through the stable-ID API."""
    matches = tuple(
        account for account in store.saved_accounts() if account.label == label
    )
    if len(matches) != 1:
        raise AssertionError("Expected one saved test account.")
    store.remove_saved(matches[0].account_id, expected=matches[0])


def seed_finalized_selections(
    paths: ApplicationPaths,
    *states: FinalizedSelection,
) -> None:
    """Publish one validated absent-only finalized-selection fixture."""
    PrivateFilesystem(paths.selected_state).commit_opaque_private(
        encode_selected_state(SelectedStateDocument(states)),
        expected_source=AuthorityExpectation.ABSENT,
    )

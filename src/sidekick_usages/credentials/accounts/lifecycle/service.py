"""Crash-recoverable saved-account and provider-profile retirement."""

from sidekick_usages.core.accounts.models import SavedAccount
from sidekick_usages.core.accounts.types import SidekickAccountId
from sidekick_usages.core.selection.types import (
    OperationState,
    ProviderRuntimeState,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.accounts.lifecycle.models import (
    AccountLifecyclePersistence,
    AccountLifecycleRuntime,
    AccountProfileFailure,
    AccountRemovalFailure,
    AccountRemovalFailureKind,
    AccountRemovalPartialFailure,
    AccountRemovalResult,
    AccountRemovalSuccess,
)
from sidekick_usages.credentials.claude.managed.profile import (
    prepare_claude_managed_profile,
)
from sidekick_usages.paths import (
    ApplicationPaths,
    managed_claude_config_dir,
    managed_codex_home,
)
from sidekick_usages.persistence.accounts.removal.models import (
    AccountRemovalRecord,
)
from sidekick_usages.persistence.accounts.removal.store import (
    AccountRemovalStore,
)
from sidekick_usages.persistence.errors import (
    PersistenceError,
    SourceChangedError,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.state.files import (
    ManagedStateConflictError,
    ManagedStateConflictKind,
)
from sidekick_usages.providers.claude.errors import ClaudeProcessError
from sidekick_usages.providers.claude.managed.errors import ClaudeManagedError
from sidekick_usages.providers.claude.managed.logout.service import (
    logout_managed_claude_profile,
)

_FAILURE_MESSAGES = {
    AccountRemovalFailureKind.MISSING: "The saved account no longer exists.",
    AccountRemovalFailureKind.SELECTED: (
        "The selected account cannot be removed."
    ),
    AccountRemovalFailureKind.ACTIVE_ACTIVATION: (
        "An active account switch must finish before removal."
    ),
    AccountRemovalFailureKind.RUNNING_OPERATION: (
        "A running account operation must finish before removal."
    ),
    AccountRemovalFailureKind.PROVIDER_UNAVAILABLE: (
        "Official Claude logout could not be completed and verified."
    ),
    AccountRemovalFailureKind.PROFILE_UNSAFE: (
        "The Sidekick-owned provider profile cannot be retired safely."
    ),
    AccountRemovalFailureKind.PROFILE_UNMAPPABLE: (
        "A managed provider profile does not map to a stable account ID."
    ),
    AccountRemovalFailureKind.RECONCILIATION_REQUIRED: (
        "Removal stopped because saved account state changed after "
        "provider cleanup."
    ),
    AccountRemovalFailureKind.STATE_CHANGED: (
        "The saved account changed while removal was in progress."
    ),
    AccountRemovalFailureKind.PERSISTENCE_UNAVAILABLE: (
        "Saved account state cannot be changed safely."
    ),
    AccountRemovalFailureKind.PROFILE_CLEANUP_FAILED: (
        "The account was removed, but its Sidekick-owned profile remains."
    ),
}
_CONFLICT_FAILURES = {
    ManagedStateConflictKind.SELECTED_ACCOUNT: (
        AccountRemovalFailureKind.SELECTED
    ),
    ManagedStateConflictKind.ACTIVE_ACTIVATION: (
        AccountRemovalFailureKind.ACTIVE_ACTIVATION
    ),
    ManagedStateConflictKind.RUNNING_OPERATION: (
        AccountRemovalFailureKind.RUNNING_OPERATION
    ),
    ManagedStateConflictKind.CONCURRENT_CHANGE: (
        AccountRemovalFailureKind.STATE_CHANGED
    ),
}
_RETRYABLE_FAILURES = frozenset(
    {
        AccountRemovalFailureKind.ACTIVE_ACTIVATION,
        AccountRemovalFailureKind.RUNNING_OPERATION,
        AccountRemovalFailureKind.STATE_CHANGED,
        AccountRemovalFailureKind.PERSISTENCE_UNAVAILABLE,
        AccountRemovalFailureKind.PROFILE_CLEANUP_FAILED,
    }
)


class AccountLifecycleCoordinator:
    """Retire accounts through durable, provider-aware crash boundaries."""

    def __init__(
        self,
        paths: ApplicationPaths,
        persistence: AccountLifecyclePersistence,
        *,
        runtime: AccountLifecycleRuntime | None = None,
    ) -> None:
        resolved_runtime = (
            AccountLifecycleRuntime() if runtime is None else runtime
        )
        if (
            persistence.claude_profiles.root != paths.private_claude_profiles
            or persistence.codex_profiles.root != paths.private_codex_profiles
        ):
            raise ValueError("Lifecycle profile trees do not match app paths.")
        self._paths = paths
        self._persistence = persistence
        self._removals = AccountRemovalStore(paths.durable_operations)
        self._environment = resolved_runtime.environment
        self._host = resolved_runtime.host
        self._runner = resolved_runtime.runner

    def remove(
        self,
        account_id: SidekickAccountId,
    ) -> AccountRemovalResult:
        """Start or resume removal of one exact stable saved account."""
        pending = self._removals.get(account_id)
        if pending is not None:
            return self._retry(pending)
        account = self._target(account_id)
        if isinstance(account, AccountRemovalFailure):
            return account
        return self._start(account)

    def reconcile(
        self,
        *,
        excluding_account_id: SidekickAccountId | None = None,
    ) -> tuple[AccountRemovalResult, ...]:
        """Resume durable removals outside an explicit retry target."""
        results: list[AccountRemovalResult] = []
        for record in self._removals.load():
            if record.account_id == excluding_account_id:
                continue
            results.append(self._resume(record))
        return tuple(results)

    def reset_provider(
        self,
        provider_id: ProviderId,
    ) -> tuple[AccountRemovalResult, ...]:
        """Remove saved accounts and every qualified orphan profile."""
        return self._reset((provider_id,))

    def reset_all(self) -> tuple[AccountRemovalResult, ...]:
        """Remove all saved accounts and qualified provider profiles."""
        return self._reset(tuple(ProviderId))

    def _reset(
        self,
        providers: tuple[ProviderId, ...],
    ) -> tuple[AccountRemovalResult, ...]:
        results: list[AccountRemovalResult] = []
        pending = tuple(
            record
            for record in self._removals.load()
            if record.provider_id in providers
        )
        pending_ids = {record.account_id for record in pending}
        for record in pending:
            results.append(self._retry(record))
        accounts = tuple(
            account
            for account in self._persistence.accounts.saved_accounts()
            if account.provider_id in providers
            and account.account_id not in pending_ids
        )
        results.extend(self.remove(account.account_id) for account in accounts)
        for provider_id in providers:
            results.extend(
                self._retire_orphan_profiles(
                    provider_id,
                    frozenset(pending_ids),
                )
            )
        return tuple(results)

    def _start(self, account: SavedAccount) -> AccountRemovalResult:
        account_id = account.account_id
        record: AccountRemovalRecord | None = None
        try:
            with self._persistence.activations.account_removal_guard(
                account_id
            ):
                current = self._persistence.accounts.read_saved(account_id)
                if current != account:
                    return _failure(
                        account_id,
                        AccountRemovalFailureKind.STATE_CHANGED,
                    )
                conflict = self._preflight(account)
                if conflict is not None:
                    return conflict
                profile_required = self._profile_required(account)
                record = self._removals.prepare(
                    account,
                    profile_retired=not profile_required,
                )
                return self._resume_locked(record, account)
        except ManagedStateConflictError as error:
            return self._record_failure(
                record,
                account,
                _CONFLICT_FAILURES[error.kind],
            )
        except SourceChangedError:
            return self._record_failure(
                record,
                account,
                AccountRemovalFailureKind.STATE_CHANGED,
            )
        except PersistenceError:
            return self._record_failure(
                record,
                account,
                AccountRemovalFailureKind.PERSISTENCE_UNAVAILABLE,
            )

    def _retry(
        self,
        record: AccountRemovalRecord,
    ) -> AccountRemovalResult:
        account: SavedAccount | None = None
        try:
            with self._persistence.activations.account_removal_guard(
                record.account_id
            ):
                account = self._persistence.accounts.read_saved(
                    record.account_id
                )
                if account is not None and not self._removals.matches(
                    record, account
                ):
                    conflict = self._preflight(account)
                    if conflict is not None:
                        return conflict
                    record = self._removals.reauthorize(
                        record,
                        account,
                        profile_retired=not self._profile_required(account),
                    )
                return self._resume_locked(record, account)
        except ManagedStateConflictError as error:
            return self._record_failure(
                record,
                account,
                _CONFLICT_FAILURES[error.kind],
            )
        except SourceChangedError:
            return self._record_failure(
                record,
                account,
                AccountRemovalFailureKind.STATE_CHANGED,
            )
        except PersistenceError:
            return self._record_failure(
                record,
                account,
                AccountRemovalFailureKind.PERSISTENCE_UNAVAILABLE,
            )

    def _resume(
        self,
        record: AccountRemovalRecord,
    ) -> AccountRemovalResult:
        account: SavedAccount | None = None
        try:
            with self._persistence.activations.account_removal_guard(
                record.account_id
            ):
                account = self._persistence.accounts.read_saved(
                    record.account_id
                )
                return self._resume_locked(record, account)
        except ManagedStateConflictError as error:
            return self._record_failure(
                record,
                account,
                _CONFLICT_FAILURES[error.kind],
            )
        except SourceChangedError:
            return self._record_failure(
                record,
                account,
                AccountRemovalFailureKind.STATE_CHANGED,
            )
        except PersistenceError:
            return self._record_failure(
                record,
                account,
                AccountRemovalFailureKind.PERSISTENCE_UNAVAILABLE,
            )

    def _resume_locked(
        self,
        record: AccountRemovalRecord,
        account: SavedAccount | None,
    ) -> AccountRemovalResult:
        mismatch = self._record_mismatch(record, account)
        if mismatch is not None:
            return mismatch
        if account is None and not record.phase.metadata_removed:
            record = self._removals.mark_metadata_removed(record)
        conflict = None if account is None else self._preflight(account)
        if conflict is not None:
            return conflict
        outcome = self._advance_claude_retirement(record, account)
        if not isinstance(outcome, AccountRemovalRecord):
            return outcome
        outcome = self._advance_metadata_removal(outcome, account)
        if not isinstance(outcome, AccountRemovalRecord):
            return outcome
        outcome = self._advance_codex_retirement(outcome, account)
        if not isinstance(outcome, AccountRemovalRecord):
            return outcome
        record = outcome
        self._finalize(record)
        return (
            AccountRemovalSuccess.from_record(record)
            if account is None
            else AccountRemovalSuccess.from_account(account)
        )

    def _advance_claude_retirement(
        self,
        record: AccountRemovalRecord,
        account: SavedAccount | None,
    ) -> (
        AccountRemovalRecord
        | AccountRemovalFailure
        | AccountRemovalPartialFailure
    ):
        if (
            record.provider_id is not ProviderId.CLAUDE
            or record.phase.profile_retired
        ):
            return record
        failure = self._retire_claude_profile(record.account_id)
        if failure is not None:
            return self._record_failure(record, account, failure.kind)
        return self._removals.mark_profile_retired(record)

    def _advance_metadata_removal(
        self,
        record: AccountRemovalRecord,
        account: SavedAccount | None,
    ) -> AccountRemovalRecord | AccountRemovalFailure:
        if record.phase.metadata_removed:
            return record
        if account is None:
            raise AssertionError("Removal metadata state is inconsistent.")
        conflict = self._preflight(account)
        if conflict is not None:
            return conflict
        return self._remove_metadata(record, account)

    def _advance_codex_retirement(
        self,
        record: AccountRemovalRecord,
        account: SavedAccount | None,
    ) -> AccountRemovalRecord | AccountRemovalPartialFailure:
        if (
            record.provider_id is not ProviderId.CODEX
            or record.phase.profile_retired
        ):
            return record
        failure = self._retire_codex_profile(record, account)
        if failure is not None:
            return failure
        return self._removals.mark_profile_retired(record)

    def _remove_metadata(
        self,
        record: AccountRemovalRecord,
        account: SavedAccount,
    ) -> AccountRemovalRecord | AccountRemovalFailure:
        try:
            self._persistence.accounts.remove_saved(
                account.account_id,
                expected=account,
            )
        except SourceChangedError:
            current = self._persistence.accounts.read_saved(account.account_id)
            if current is not None:
                return _failure(
                    account.account_id,
                    AccountRemovalFailureKind.RECONCILIATION_REQUIRED,
                )
        return self._removals.mark_metadata_removed(record)

    def _finalize(self, record: AccountRemovalRecord) -> None:
        if (
            not record.phase.metadata_removed
            or not record.phase.profile_retired
            or self._persistence.accounts.read_saved(record.account_id)
            is not None
        ):
            raise SourceChangedError
        self._persistence.selected.remove_account(record.account_id)
        self._persistence.operations.remove_account(record.account_id)
        self._removals.delete(record)

    def _record_mismatch(
        self,
        record: AccountRemovalRecord,
        account: SavedAccount | None,
    ) -> AccountRemovalFailure | None:
        if record.phase.metadata_removed:
            return (
                None
                if account is None
                else _failure(
                    record.account_id,
                    AccountRemovalFailureKind.RECONCILIATION_REQUIRED,
                )
            )
        if account is None:
            return None
        if not self._removals.matches(record, account):
            return _failure(
                record.account_id,
                AccountRemovalFailureKind.RECONCILIATION_REQUIRED,
            )
        return None

    def _record_failure(
        self,
        record: AccountRemovalRecord | None,
        account: SavedAccount | None,
        kind: AccountRemovalFailureKind,
    ) -> AccountRemovalFailure | AccountRemovalPartialFailure:
        if record is None:
            if account is None:
                raise AssertionError("Account failure lost its stable ID.")
            return _failure(account.account_id, kind)
        try:
            metadata_removed = (
                self._persistence.accounts.read_saved(record.account_id)
                is None
            )
        except PersistenceError:
            metadata_removed = record.phase.metadata_removed
            try:
                current_record = self._removals.get(record.account_id)
            except PersistenceError:
                current_record = None
            metadata_removed = metadata_removed or (
                current_record is not None
                and current_record.phase.metadata_removed
            )
        if not metadata_removed:
            return _failure(record.account_id, kind)
        return AccountRemovalPartialFailure(
            record.account_id,
            None if account is None else account.label,
            record.provider_id,
            kind,
            _FAILURE_MESSAGES[kind],
            kind not in _RETRYABLE_FAILURES,
        )

    def _preflight(
        self,
        account: SavedAccount,
    ) -> AccountRemovalFailure | None:
        selected = self._persistence.selected.load(account.provider_id)
        if (
            selected is not None
            and selected.runtime_state is ProviderRuntimeState.SAVED_ACTIVE
            and selected.account_id == account.account_id
        ):
            return _failure(
                account.account_id,
                AccountRemovalFailureKind.SELECTED,
            )
        if any(
            operation.account_id == account.account_id
            and operation.state is OperationState.RUNNING
            for operation in self._persistence.operations.load()
        ):
            return _failure(
                account.account_id,
                AccountRemovalFailureKind.RUNNING_OPERATION,
            )
        return None

    def _profile_required(self, account: SavedAccount) -> bool:
        if account.provider_id is ProviderId.CODEX:
            return True
        profile = managed_claude_config_dir(self._paths, account.account_id)
        return account.has_managed_authority or profile in (
            self._persistence.claude_profiles.list_owned_directories_shallow()
        )

    def _retire_claude_profile(
        self,
        account_id: SidekickAccountId,
    ) -> AccountRemovalFailure | None:
        try:
            profile = managed_claude_config_dir(self._paths, account_id)
            capabilities = prepare_claude_managed_profile(
                self._paths,
                self._persistence.claude_profiles,
                account_id,
                environment=self._environment,
                host=self._host,
                runner=self._runner,
            )
            logout_managed_claude_profile(
                capabilities,
                self._environment,
                runner=self._runner,
            )
            self._persistence.claude_profiles.destroy_owned_directory(profile)
        except ClaudeManagedError, ClaudeProcessError:
            return _failure(
                account_id,
                AccountRemovalFailureKind.PROVIDER_UNAVAILABLE,
            )
        except PersistenceError, ValueError:
            return _failure(
                account_id,
                AccountRemovalFailureKind.PROFILE_UNSAFE,
            )
        return None

    def _retire_codex_profile(
        self,
        record: AccountRemovalRecord,
        account: SavedAccount | None,
    ) -> AccountRemovalPartialFailure | None:
        try:
            self._persistence.codex_profiles.destroy_owned_directory(
                managed_codex_home(self._paths, record.account_id)
            )
        except PersistenceError, ValueError:
            return AccountRemovalPartialFailure(
                record.account_id,
                None if account is None else account.label,
                record.provider_id,
                AccountRemovalFailureKind.PROFILE_CLEANUP_FAILED,
                _FAILURE_MESSAGES[
                    AccountRemovalFailureKind.PROFILE_CLEANUP_FAILED
                ],
                False,
            )
        return None

    def _retire_orphan_profiles(
        self,
        provider_id: ProviderId,
        attempted_ids: frozenset[SidekickAccountId],
    ) -> tuple[AccountRemovalResult, ...]:
        tree = self._profile_tree(provider_id)
        try:
            directories = tree.list_owned_directories_shallow()
        except PersistenceError:
            return (
                _profile_failure(
                    provider_id,
                    tree.root.name,
                    AccountRemovalFailureKind.PROFILE_UNSAFE,
                ),
            )
        results: list[AccountRemovalResult] = []
        for directory in directories:
            try:
                account_id = SidekickAccountId(directory.name)
            except ValueError:
                results.append(
                    _profile_failure(
                        provider_id,
                        directory.name,
                        AccountRemovalFailureKind.PROFILE_UNMAPPABLE,
                    )
                )
                continue
            if account_id in attempted_ids:
                continue
            if self._persistence.accounts.read_saved(account_id) is not None:
                continue
            record = self._removals.get(account_id)
            if record is not None and record.provider_id is not provider_id:
                results.append(
                    _profile_failure(
                        provider_id,
                        directory.name,
                        AccountRemovalFailureKind.PROFILE_UNSAFE,
                    )
                )
                continue
            if record is None:
                record = self._removals.prepare_orphan(
                    account_id,
                    provider_id,
                )
            results.append(self._resume(record))
        return tuple(results)

    def _profile_tree(
        self,
        provider_id: ProviderId,
    ) -> PrivateCredentialTree:
        return (
            self._persistence.claude_profiles
            if provider_id is ProviderId.CLAUDE
            else self._persistence.codex_profiles
        )

    def _target(
        self,
        account_id: SidekickAccountId,
    ) -> SavedAccount | AccountRemovalFailure:
        try:
            account = self._persistence.accounts.read_saved(account_id)
        except PersistenceError:
            return _failure(
                account_id,
                AccountRemovalFailureKind.PERSISTENCE_UNAVAILABLE,
            )
        if account is None:
            return _failure(
                account_id,
                AccountRemovalFailureKind.MISSING,
            )
        return account


def _failure(
    account_id: SidekickAccountId,
    kind: AccountRemovalFailureKind,
) -> AccountRemovalFailure:
    return AccountRemovalFailure(
        account_id,
        kind,
        _FAILURE_MESSAGES[kind],
        kind not in _RETRYABLE_FAILURES,
    )


def _profile_failure(
    provider_id: ProviderId,
    basename: str,
    kind: AccountRemovalFailureKind,
) -> AccountProfileFailure:
    return AccountProfileFailure(
        provider_id,
        basename,
        kind,
        _FAILURE_MESSAGES[kind],
        True,
    )

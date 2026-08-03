"""Synthetic state for verified native Claude activation."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialAction,
    CredentialHealth,
    OperationId,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.selection.models import (
    DueOperation,
    FinalizedSelection,
    SelectionEpoch,
)
from sidekick_usages.core.selection.types import (
    OperationKind,
    OperationPriority,
    OperationState,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.claude.activation.authority import (
    ClaudeActivationAuthorityCoordinator,
)
from sidekick_usages.credentials.claude.activation.models import (
    ClaudeActivationRuntime,
)
from sidekick_usages.credentials.claude.activation.reconciliation import (
    ClaudeNativeReconciliationService,
)
from sidekick_usages.credentials.claude.activation.recovery import (
    ClaudeActivationRecoveryService,
)
from sidekick_usages.credentials.claude.activation.service import (
    ClaudeActivationService,
)
from sidekick_usages.credentials.claude.managed.profile import (
    ClaudeProfileCapabilityFactory,
)
from sidekick_usages.daemon.worker.claude.selection import (
    ClaudeSelectionWorkerExecutor,
)
from sidekick_usages.paths import ApplicationPaths
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
from sidekick_usages.persistence.supervisor.activation import (
    ActivationJournalStore,
)
from sidekick_usages.persistence.supervisor.selection import SelectedStateStore
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from sidekick_usages.platform.types import HostPlatform
from sidekick_usages.providers.claude.activation.types import (
    ClaudeRemoteControlState,
)
from sidekick_usages.providers.claude.auth.generation import (
    claude_access_token_generation,
)
from sidekick_usages.providers.claude.auth.storage.service import (
    CLAUDE_CREDENTIAL_FILE,
)
from sidekick_usages.providers.claude.models import (
    ClaudeCommandResult,
    ClaudeNativeProfile,
)
from tests.fakes.claude.managed import (
    ClaudeCommandScript,
    ClaudeManagedLoginScript,
    ClaudeRunner,
    claude_profile_status,
    credential_payload,
    managed_profile,
    native_profile,
    profile_tree,
)
from tests.support.persistence import (
    make_application_paths,
    seed_finalized_selections,
)
from tests.support.time import REFERENCE_TIME, FixedClock

_SOURCE_ACCOUNT_ID = SidekickAccountId("11111111-1111-4111-8111-111111111111")
_SOURCE_AUTHORITY_ID = AuthorityId("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_TARGET_ACCOUNT_ID = SidekickAccountId("22222222-2222-4222-8222-222222222222")
_TARGET_AUTHORITY_ID = AuthorityId("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_KNOWN_ACCOUNT_ID = SidekickAccountId("33333333-3333-4333-8333-333333333333")
_KNOWN_AUTHORITY_ID = AuthorityId("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_CODEX_ACCOUNT_ID = SidekickAccountId("44444444-4444-4444-8444-444444444444")
_ACTIVATION_OPERATION_ID = OperationId("55555555-5555-4555-8555-555555555555")
_RECOVERY_OPERATION_ID = OperationId("66666666-6666-4666-8666-666666666666")
_RETRY_OPERATION_ID = OperationId("77777777-7777-4777-8777-777777777777")
_NATIVE_RECONCILIATION_OPERATION_ID = OperationId(
    "88888888-8888-4888-8888-888888888888"
)
_SOURCE_STATUS, _SOURCE_IDENTITY = claude_profile_status("source")
_TARGET_STATUS, _TARGET_IDENTITY = claude_profile_status("target")
_KNOWN_STATUS, _KNOWN_IDENTITY = claude_profile_status("known")
_UNKNOWN_STATUS, _UNKNOWN_IDENTITY = claude_profile_status("unknown")
_INITIAL_ACCESS_EXPIRY = REFERENCE_TIME + timedelta(hours=2)
_RETAINED_ACCESS_EXPIRY = REFERENCE_TIME + timedelta(hours=3)
_NATIVE_TARGET_ACCESS_EXPIRY = REFERENCE_TIME + timedelta(hours=4)
_ROLLBACK_ACCESS_EXPIRY = REFERENCE_TIME + timedelta(hours=5)
_EXTERNAL_ACCESS_EXPIRY = REFERENCE_TIME + timedelta(hours=6)
_EXPIRED_REFRESH = REFERENCE_TIME - timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class ClaudeActivationScenario:
    """Complete synthetic state for one healthy native activation."""

    paths: ApplicationPaths
    source: SavedAccount
    target: SavedAccount
    store: AccountStore
    profiles: PrivateCredentialTree
    source_profile: Path
    target_profile: Path
    target_payload: bytes
    retained_source_payload: bytes
    native_target_payload: bytes
    native: ClaudeNativeProfile
    native_credentials: Path
    script: ClaudeManagedLoginScript
    runner: ClaudeRunner
    selected: SelectedStateStore
    codex_state: FinalizedSelection
    journals: ActivationJournalStore
    executor: ClaudeSelectionWorkerExecutor
    operation: DueOperation
    environment: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ClaudeRecoveryScenario:
    """Synthetic interrupted activation and deterministic recovery state."""

    paths: ApplicationPaths
    source: SavedAccount
    target: SavedAccount
    known: SavedAccount
    store: AccountStore
    profiles: PrivateCredentialTree
    source_profile: Path
    target_profile: Path
    target_payload: bytes
    retained_source_payload: bytes
    native_target_payload: bytes
    native_rollback_payload: bytes
    known_native_payload: bytes
    unknown_native_payload: bytes
    native: ClaudeNativeProfile
    native_credentials: Path
    script: ClaudeManagedLoginScript
    runner: ClaudeRunner
    selected: SelectedStateStore
    codex_state: FinalizedSelection
    journals: ActivationJournalStore
    executor: ClaudeSelectionWorkerExecutor
    activation: DueOperation
    recovery: DueOperation
    retry: DueOperation
    native_reconciliation: DueOperation
    account_ids: tuple[SidekickAccountId, ...]


@dataclass(frozen=True, slots=True)
class _ClaudeRuntimeFixture:
    """Shared provider-neutral runtime stores for activation scenarios."""

    runner: ClaudeRunner
    selected: SelectedStateStore
    codex_state: FinalizedSelection
    journals: ActivationJournalStore
    executor: ClaudeSelectionWorkerExecutor
    environment: Mapping[str, str]


class _InterruptAfterNativeLogin:
    """Raise one supplied interruption after native credentials are written."""

    def __init__(
        self,
        script: ClaudeManagedLoginScript,
        native_directory: Path,
        interruption: BaseException,
    ) -> None:
        self._script = script
        self._native_directory = native_directory
        self._interruption = interruption
        self._interrupted = False

    def __call__(
        self,
        arguments: tuple[str, ...],
        environment: dict[str, str] | None,
        working_directory: Path | None,
    ) -> ClaudeCommandResult:
        """Interrupt once after the official native login process returns."""
        result = self._script(
            arguments,
            environment,
            working_directory,
        )
        if (
            not self._interrupted
            and arguments == ("auth", "login", "--claudeai")
            and self._script.login_profiles[-1] == self._native_directory
        ):
            self._interrupted = True
            raise self._interruption
        return result


def claude_activation_scenario(
    root: Path,
    *,
    environment: dict[str, str] | None = None,
    native_logged_out: bool = False,
    interrupt_after_native_login: BaseException | None = None,
    remote_control: ClaudeRemoteControlState = (
        ClaudeRemoteControlState.PROOF_UNAVAILABLE
    ),
    status_only_native_login: bool = False,
    advance_native_mtime: bool = True,
) -> ClaudeActivationScenario:
    """Build one healthy A-to-B official Claude activation scenario."""
    source_payload = credential_payload(
        None,
        None,
        token_suffix="source-private",
        access_expires_at=_INITIAL_ACCESS_EXPIRY,
    )
    target_payload = credential_payload(
        None,
        None,
        token_suffix="target-private",
        access_expires_at=_INITIAL_ACCESS_EXPIRY,
    )
    retained_source_payload = credential_payload(
        None,
        None,
        token_suffix="source-retained",
        access_expires_at=_RETAINED_ACCESS_EXPIRY,
    )
    native_source_payload = credential_payload(
        None,
        None,
        token_suffix="source-native",
        access_expires_at=_INITIAL_ACCESS_EXPIRY,
    )
    native_target_payload = credential_payload(
        None,
        None,
        token_suffix="target-native",
        access_expires_at=_NATIVE_TARGET_ACCESS_EXPIRY,
    )
    source = _managed_saved_account(
        _SOURCE_ACCOUNT_ID,
        _SOURCE_AUTHORITY_ID,
        "source",
        _SOURCE_IDENTITY,
        "source-private",
        _INITIAL_ACCESS_EXPIRY,
    )
    target = _managed_saved_account(
        _TARGET_ACCOUNT_ID,
        _TARGET_AUTHORITY_ID,
        "target",
        _TARGET_IDENTITY,
        "target-private",
        _INITIAL_ACCESS_EXPIRY,
    )
    paths, store, profiles = _seed_managed_accounts(
        root,
        (source, target),
        {
            source.account_id: source_payload,
            target.account_id: target_payload,
        },
    )
    native = native_profile(root / "native-home")
    native_credentials = native.config_directory / CLAUDE_CREDENTIAL_FILE
    source_profile = managed_profile(
        paths,
        source.account_id,
    ).config_directory
    target_profile = managed_profile(
        paths,
        target.account_id,
    ).config_directory
    script = ClaudeManagedLoginScript(
        profiles,
        {
            source_profile: (retained_source_payload,),
            native.config_directory: (
                (
                    native_source_payload
                    if status_only_native_login
                    else native_target_payload
                ),
            ),
        },
        profile_statuses={
            source_profile: _SOURCE_STATUS,
            target_profile: _TARGET_STATUS,
            native.config_directory: _SOURCE_STATUS,
        },
        refresh_statuses={
            source_profile: (_SOURCE_STATUS,),
            native.config_directory: (_TARGET_STATUS,),
        },
        advance_native_mtime=advance_native_mtime,
    )
    if not native_logged_out:
        script.set_authority(
            native.config_directory,
            native_source_payload,
            _SOURCE_STATUS,
        )
    runtime_script: ClaudeCommandScript = script
    if interrupt_after_native_login is not None:
        runtime_script = _InterruptAfterNativeLogin(
            script,
            native.config_directory,
            interrupt_after_native_login,
        )
    runtime = _runtime_fixture(
        paths,
        store,
        profiles,
        native,
        source,
        runtime_script,
        environment=environment,
        remote_control=remote_control,
    )
    operation = _selection_operation(
        _ACTIVATION_OPERATION_ID,
        target.account_id,
        OperationKind.ACTIVATE,
    )
    return ClaudeActivationScenario(
        paths=paths,
        source=source,
        target=target,
        store=store,
        profiles=profiles,
        source_profile=source_profile,
        target_profile=target_profile,
        target_payload=target_payload,
        retained_source_payload=retained_source_payload,
        native_target_payload=native_target_payload,
        native=native,
        native_credentials=native_credentials,
        script=script,
        runner=runtime.runner,
        selected=runtime.selected,
        codex_state=runtime.codex_state,
        journals=runtime.journals,
        executor=runtime.executor,
        operation=operation,
        environment=runtime.environment,
    )


def claude_recovery_scenario(
    root: Path,
    interruption: BaseException,
    *,
    rollback_succeeds: bool,
) -> ClaudeRecoveryScenario:
    """Build one interrupted native mutation with a single rollback result."""
    source_payload = credential_payload(
        None,
        None,
        token_suffix="source-private",
        access_expires_at=_INITIAL_ACCESS_EXPIRY,
    )
    target_payload = credential_payload(
        None,
        None,
        token_suffix="target-private",
        access_expires_at=_INITIAL_ACCESS_EXPIRY,
    )
    known_payload = credential_payload(
        None,
        None,
        token_suffix="known-private",
        access_expires_at=_INITIAL_ACCESS_EXPIRY,
    )
    retained_source_payload = credential_payload(
        None,
        None,
        token_suffix="source-retained",
        access_expires_at=_RETAINED_ACCESS_EXPIRY,
    )
    native_source_payload = credential_payload(
        None,
        None,
        token_suffix="source-native",
        access_expires_at=_INITIAL_ACCESS_EXPIRY,
    )
    native_target_payload = credential_payload(
        None,
        None,
        token_suffix="target-interrupted",
        access_expires_at=_NATIVE_TARGET_ACCESS_EXPIRY,
        refresh_expires_at=_EXPIRED_REFRESH,
    )
    native_rollback_payload = credential_payload(
        None,
        None,
        token_suffix="source-rollback",
        access_expires_at=_ROLLBACK_ACCESS_EXPIRY,
    )
    known_native_payload = credential_payload(
        None,
        None,
        token_suffix="known-native",
        access_expires_at=_EXTERNAL_ACCESS_EXPIRY,
    )
    unknown_native_payload = credential_payload(
        None,
        None,
        token_suffix="unknown-native",
        access_expires_at=_EXTERNAL_ACCESS_EXPIRY,
    )
    source = _managed_saved_account(
        _SOURCE_ACCOUNT_ID,
        _SOURCE_AUTHORITY_ID,
        "source",
        _SOURCE_IDENTITY,
        "source-private",
        _INITIAL_ACCESS_EXPIRY,
    )
    target = _managed_saved_account(
        _TARGET_ACCOUNT_ID,
        _TARGET_AUTHORITY_ID,
        "target",
        _TARGET_IDENTITY,
        "target-private",
        _INITIAL_ACCESS_EXPIRY,
    )
    known = _managed_saved_account(
        _KNOWN_ACCOUNT_ID,
        _KNOWN_AUTHORITY_ID,
        "known",
        _KNOWN_IDENTITY,
        "known-private",
        _INITIAL_ACCESS_EXPIRY,
    )
    paths, store, profiles = _seed_managed_accounts(
        root,
        (source, target, known),
        {
            source.account_id: source_payload,
            target.account_id: target_payload,
            known.account_id: known_payload,
        },
    )
    native = native_profile(root / "native-home")
    native_credentials = native.config_directory / CLAUDE_CREDENTIAL_FILE
    source_profile = managed_profile(
        paths,
        source.account_id,
    ).config_directory
    target_profile = managed_profile(
        paths,
        target.account_id,
    ).config_directory
    script = ClaudeManagedLoginScript(
        profiles,
        {
            source_profile: (retained_source_payload,),
            native.config_directory: (
                native_target_payload,
                native_rollback_payload if rollback_succeeds else None,
            ),
        },
        profile_statuses={
            source_profile: _SOURCE_STATUS,
            target_profile: _TARGET_STATUS,
            managed_profile(
                paths,
                known.account_id,
            ).config_directory: _KNOWN_STATUS,
            native.config_directory: _SOURCE_STATUS,
        },
        refresh_statuses={
            source_profile: (_SOURCE_STATUS,),
            native.config_directory: (
                _TARGET_STATUS,
                _SOURCE_STATUS,
            ),
        },
    )
    script.set_authority(
        native.config_directory,
        native_source_payload,
        _SOURCE_STATUS,
    )
    interrupted = _InterruptAfterNativeLogin(
        script,
        native.config_directory,
        interruption,
    )
    runtime = _runtime_fixture(
        paths,
        store,
        profiles,
        native,
        source,
        interrupted,
    )
    activation = _selection_operation(
        _ACTIVATION_OPERATION_ID,
        target.account_id,
        OperationKind.ACTIVATE,
    )
    recovery = _selection_operation(
        _RECOVERY_OPERATION_ID,
        target.account_id,
        OperationKind.RECONCILE,
    )
    retry = _selection_operation(
        _RETRY_OPERATION_ID,
        target.account_id,
        OperationKind.RECONCILE,
    )
    native_reconciliation = _selection_operation(
        _NATIVE_RECONCILIATION_OPERATION_ID,
        None,
        OperationKind.RECONCILE_NATIVE,
    )
    return ClaudeRecoveryScenario(
        paths=paths,
        source=source,
        target=target,
        known=known,
        store=store,
        profiles=profiles,
        source_profile=source_profile,
        target_profile=target_profile,
        target_payload=target_payload,
        retained_source_payload=retained_source_payload,
        native_target_payload=native_target_payload,
        native_rollback_payload=native_rollback_payload,
        known_native_payload=known_native_payload,
        unknown_native_payload=unknown_native_payload,
        native=native,
        native_credentials=native_credentials,
        script=script,
        runner=runtime.runner,
        selected=runtime.selected,
        codex_state=runtime.codex_state,
        journals=runtime.journals,
        executor=runtime.executor,
        activation=activation,
        recovery=recovery,
        retry=retry,
        native_reconciliation=native_reconciliation,
        account_ids=tuple(
            sorted(
                (
                    source.account_id,
                    target.account_id,
                    known.account_id,
                )
            )
        ),
    )


def _runtime_fixture(
    paths: ApplicationPaths,
    store: AccountStore,
    profiles: PrivateCredentialTree,
    native: ClaudeNativeProfile,
    source: SavedAccount,
    script: ClaudeCommandScript,
    *,
    environment: Mapping[str, str] | None = None,
    remote_control: ClaudeRemoteControlState = (
        ClaudeRemoteControlState.PROOF_UNAVAILABLE
    ),
) -> _ClaudeRuntimeFixture:
    """Compose one synthetic Claude selection worker and durable stores."""

    def remote_control_probe() -> ClaudeRemoteControlState:
        """Return the injected structured capability state."""
        return remote_control

    runner = ClaudeRunner(script=script)
    source_environment = (
        {
            "HOME": str(native.config_directory.parent),
            "PATH": os.defpath,
            "USER": "sidekick-test",
        }
        if environment is None
        else environment
    )
    if source_environment.get("HOME") != str(native.config_directory.parent):
        raise ValueError("Synthetic native Claude home is inconsistent.")
    selected = SelectedStateStore(paths.selected_state)
    claude_state = FinalizedSelection(
        provider_id=ProviderId.CLAUDE,
        account_id=source.account_id,
        epoch=SelectionEpoch(0),
        generation=claude_access_token_generation(
            "sk-ant-oat01-source-native"
        ),
        finalized_at=REFERENCE_TIME,
    )
    codex_state = FinalizedSelection(
        provider_id=ProviderId.CODEX,
        account_id=_CODEX_ACCOUNT_ID,
        epoch=SelectionEpoch(0),
        generation=AuthorityGeneration("codex-generation"),
        finalized_at=REFERENCE_TIME,
    )
    seed_finalized_selections(paths, claude_state, codex_state)
    journals = ActivationJournalStore(
        paths.activation_journals,
        paths.durable_operations,
    )
    clock = FixedClock()
    runtime = ClaudeActivationRuntime(
        environment=source_environment,
        host=HostPlatform.LINUX,
        runner=runner,
        remote_control_probe=remote_control_probe,
    )
    capabilities = ClaudeProfileCapabilityFactory(
        paths,
        profiles,
        environment=source_environment,
        host=HostPlatform.LINUX,
        runner=runner,
    )
    authorities = ClaudeActivationAuthorityCoordinator(
        paths,
        store,
        profiles,
        clock,
        capabilities=capabilities,
        runtime=runtime,
    )
    recovery = ClaudeActivationRecoveryService(
        authorities,
        journals,
        selected,
        clock,
    )
    executor = ClaudeSelectionWorkerExecutor(
        ClaudeActivationService(
            authorities,
            journals,
            selected,
            clock,
        ),
        recovery,
        ClaudeNativeReconciliationService(
            authorities,
            recovery,
            journals,
            selected,
            clock,
        ),
        clock,
    )
    return _ClaudeRuntimeFixture(
        runner,
        selected,
        codex_state,
        journals,
        executor,
        source_environment,
    )


def _selection_operation(
    operation_id: OperationId,
    account_id: SidekickAccountId | None,
    kind: OperationKind,
) -> DueOperation:
    """Build one running interactive Claude selection operation."""
    return DueOperation(
        operation_id=operation_id,
        provider_id=ProviderId.CLAUDE,
        account_id=account_id,
        kind=kind,
        priority=OperationPriority.INTERACTIVE,
        state=OperationState.RUNNING,
        due_at=REFERENCE_TIME,
        updated_at=REFERENCE_TIME,
    )


def _managed_saved_account(
    account_id: SidekickAccountId,
    authority_id: AuthorityId,
    label: str,
    provider_identity: ProviderIdentity,
    token_suffix: str,
    access_expires_at: datetime,
) -> SavedAccount:
    """Build one secret-free managed Claude account."""
    return SavedAccount(
        account_id=account_id,
        label=AccountLabel(label),
        provider_id=ProviderId.CLAUDE,
        plan="pro",
        authority=ClaudeAccountAuthority(
            subscription=ClaudeManagedLoginAuthority(
                authority_id=authority_id,
                provider_identity=provider_identity,
                generation=claude_access_token_generation(
                    f"sk-ant-oat01-{token_suffix}"
                ),
                access_expires_at=access_expires_at,
                refresh_expires_at=None,
                verified_at=REFERENCE_TIME - timedelta(minutes=5),
                executable_version="2.1.220",
                health=CredentialHealth.HEALTHY,
                action=CredentialAction.NONE,
            )
        ),
        credential_health=CredentialHealth.HEALTHY,
    )


def _seed_managed_accounts(
    root: Path,
    accounts: tuple[SavedAccount, ...],
    payloads: Mapping[SidekickAccountId, bytes],
) -> tuple[ApplicationPaths, AccountStore, PrivateCredentialTree]:
    """Persist managed Claude metadata and independent private profiles."""
    paths = make_application_paths(root)
    filesystem = PersistenceFilesystem(paths.accounts)
    filesystem.repair_parent_permissions()
    with PersistenceLock(filesystem).hold() as transaction:
        transaction.commit_authority(
            encode_version_three(VersionThreeDocument(accounts)),
            AuthorityExpectation.ABSENT,
        )
    profiles = profile_tree(paths)
    for account in accounts:
        profile = managed_profile(paths, account.account_id)
        profiles.ensure_owned_directory(profile.config_directory)
        profiles.write_owned_file(
            profile.config_directory,
            CLAUDE_CREDENTIAL_FILE,
            payloads[account.account_id],
        )
    credentials = PrivateCredentialTree(
        paths.private_credentials,
        account_path=paths.accounts,
    )
    return paths, AccountStore(paths.accounts, credentials).load(), profiles

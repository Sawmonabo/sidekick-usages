"""Codex credential coordination and private persistence."""

import hashlib
import re
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from sidekick_usages.clock import Clock
from sidekick_usages.core.models import Account, CodexCredentials
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.models import (
    CredentialExportResult,
    CredentialExportSuccess,
)
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import PersistenceError
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.models.artifact import (
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.private.bundles.writes import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.types.artifact import AuthorityExpectation
from sidekick_usages.persistence.types.credential import (
    PrivateCredentialOwnership,
)
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.auth import (
    CODEX_AUTH_FILE,
    CODEX_CONFIG_FILE,
    codex_auth_path,
    default_codex_home,
    prepare_export_bundle,
    prepare_private_bundle,
    prepare_private_bundle_from_auth_bytes,
    validate_auth_bundle_matches_account,
    validate_auth_bundle_owner,
)

_BUNDLE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_BUNDLE_DIGEST_HEX_LENGTH = 32
_BUNDLE_STEM_LENGTH = 221


@dataclass(frozen=True, slots=True)
class _PrivateTarget:
    present: bool
    auth: bytes | None
    config: bytes | None


@dataclass(frozen=True, slots=True)
class PreparedCodexCredentialRefresh:
    """Validated account fields and private bundle for one rotation."""

    credentials: CodexCredentials
    plan: str | None
    private_bundle: PreparedPrivateBundleWrite


def _failure(
    kind: ProviderFailureKind,
    message: str,
) -> ProviderFailure:
    return ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=kind,
        message=message,
    )


def private_codex_home(root: Path, label: str) -> Path:
    """Return the collision-resistant private Codex home for ``label``."""
    safe = _BUNDLE_NAME_RE.sub("_", label).strip("._-") or "account"
    digest = hashlib.sha256(label.encode()).hexdigest()
    return root / (
        f"{safe[:_BUNDLE_STEM_LENGTH]}--{digest[:_BUNDLE_DIGEST_HEX_LENGTH]}"
    )


class CodexCredentialCoordinator:
    """Bridge pure Codex auth preparation to persistence boundaries."""

    def __init__(
        self,
        store: AccountStore,
        private_credentials: PrivateCredentialTree,
        *,
        clock: Clock,
    ) -> None:
        self._store = store
        self._private = private_credentials
        self._clock = clock

    def prepare_account(
        self,
        candidate: Account,
        previous: Account | None,
        *,
        source_home: Path | None,
        use_existing_source: bool,
        require_bundle: bool,
        reference_time: datetime,
    ) -> tuple[Account, PreparedPrivateBundleWrite | None] | ProviderFailure:
        """Prepare one private mutation without writing account authority."""
        target = private_codex_home(self._private.root, str(candidate.label))
        previous_target = self._previous_owns_target(previous, target)
        observed = self._inspect_target(target, previous, previous_target)
        if isinstance(observed, ProviderFailure):
            return observed
        if use_existing_source:
            prepared = prepare_private_bundle_from_auth_bytes(
                candidate,
                target,
                observed.auth,
                reference_time=reference_time,
            )
        else:
            prepared = prepare_private_bundle(
                candidate,
                target,
                source_home=source_home,
                reference_time=reference_time,
            )
        if isinstance(prepared, ProviderFailure):
            if (
                prepared.kind is ProviderFailureKind.INCOMPLETE
                and not require_bundle
                and not previous_target
            ):
                credentials = candidate.credentials
                if not isinstance(credentials, CodexCredentials):
                    raise AssertionError(
                        "Codex account has wrong credentials."
                    )
                candidate.credentials = replace(credentials, auth_home=None)
                return candidate, None
            return prepared
        candidate.credentials = prepared.credentials
        return candidate, PreparedPrivateBundleWrite(
            path=prepared.bundle_path,
            files=prepared.file_map(),
            expected_bundle_present=observed.present,
            expected_files={
                CODEX_AUTH_FILE: observed.auth,
                CODEX_CONFIG_FILE: observed.config,
            },
        )

    def prepare_refresh(
        self,
        previous: Account,
        credentials: CodexCredentials,
        plan: str | None,
        *,
        reference_time: datetime,
    ) -> PreparedCodexCredentialRefresh | ProviderFailure:
        """Prepare rotated authority and its exact private bundle together."""
        candidate = replace(
            previous,
            credentials=credentials,
            plan=previous.plan if plan is None else plan,
        )
        prepared = self.prepare_account(
            candidate,
            previous,
            source_home=None,
            use_existing_source=True,
            require_bundle=True,
            reference_time=reference_time,
        )
        if isinstance(prepared, ProviderFailure):
            return prepared
        account, private_bundle = prepared
        if private_bundle is None or not isinstance(
            account.credentials,
            CodexCredentials,
        ):
            raise AssertionError("Codex refresh preparation is incomplete.")
        return PreparedCodexCredentialRefresh(
            account.credentials,
            account.plan,
            private_bundle,
        )

    def export(
        self,
        account: Account,
        target_home: Path,
        *,
        source_home: Path | None,
    ) -> CredentialExportResult:
        """Publish config then auth and reprove source, bytes, and security."""
        try:
            return self._export_protected(
                account,
                target_home,
                source_home=source_home,
            )
        except PersistenceError:
            return _failure(
                ProviderFailureKind.UNREADABLE,
                "The Codex export target could not be protected safely.",
            )

    def _export_protected(
        self,
        account: Account,
        target_home: Path,
        *,
        source_home: Path | None,
    ) -> CredentialExportResult:
        """Run one export after translating its native failure boundary."""
        target = target_home.expanduser()
        if failure := self._validate_export_target(target, source_home):
            return failure
        config_fs = PersistenceFilesystem(target / CODEX_CONFIG_FILE)
        auth_fs = PersistenceFilesystem(target / CODEX_AUTH_FILE)
        config_fs.repair_parent_permissions()
        snapshots = {
            CODEX_CONFIG_FILE: config_fs.read_opaque_private(),
            CODEX_AUTH_FILE: auth_fs.read_opaque_private(),
        }
        existing_config = snapshots[CODEX_CONFIG_FILE]
        existing_auth = snapshots[CODEX_AUTH_FILE]
        if (
            existing_auth is not None
            and (
                ownership := validate_auth_bundle_owner(
                    existing_auth.data,
                    account.provider_account_id,
                )
            )
            is not None
        ):
            return ownership
        source_homes = self._source_homes(account, source_home)
        reference_time = self._clock.now()
        prepared = prepare_export_bundle(
            account,
            target,
            source_homes=source_homes,
            existing_config=(
                None if existing_config is None else existing_config.data
            ),
            reference_time=reference_time,
        )
        if isinstance(prepared, ProviderFailure):
            return prepared
        files = prepared.file_map()
        final_config = config_fs.commit_opaque_private(
            files[CODEX_CONFIG_FILE],
            expected_source=self._expected(snapshots[CODEX_CONFIG_FILE]),
        )
        reproved = prepare_export_bundle(
            account,
            target,
            source_homes=source_homes,
            existing_config=final_config.data,
            reference_time=reference_time,
        )
        if (
            isinstance(reproved, ProviderFailure)
            or reproved.file_map() != files
        ):
            return _failure(
                ProviderFailureKind.UNREADABLE,
                "Codex export source changed before credential publication.",
            )
        final_auth = auth_fs.commit_opaque_private(
            files[CODEX_AUTH_FILE],
            expected_source=self._expected(snapshots[CODEX_AUTH_FILE]),
        )
        final_source = prepare_export_bundle(
            account,
            target,
            source_homes=source_homes,
            existing_config=final_config.data,
            reference_time=reference_time,
        )
        final_config_proof = config_fs.read_opaque_private()
        final_auth_proof = auth_fs.read_opaque_private()
        if (
            isinstance(final_source, ProviderFailure)
            or final_source.file_map() != files
            or final_config.data != files[CODEX_CONFIG_FILE]
            or final_auth.data != files[CODEX_AUTH_FILE]
            or final_config_proof is None
            or final_config_proof.data != files[CODEX_CONFIG_FILE]
            or final_auth_proof is None
            or final_auth_proof.data != files[CODEX_AUTH_FILE]
        ):
            return _failure(
                ProviderFailureKind.UNREADABLE,
                "The Codex export could not be verified safely.",
            )
        return CredentialExportSuccess(
            account.label,
            target,
            codex_auth_path(target),
        )

    @staticmethod
    def _expected(snapshot: FileSnapshot | None) -> ExpectedAuthority:
        return (
            AuthorityExpectation.ABSENT
            if snapshot is None
            else snapshot.fingerprint
        )

    def _inspect_target(
        self,
        target: Path,
        previous: Account | None,
        previous_target: bool,
    ) -> _PrivateTarget | ProviderFailure:
        present = self._private.bundle_present(target)
        auth = (
            self._private.read_bundle_file(target, CODEX_AUTH_FILE)
            if present
            else None
        )
        config = (
            self._private.read_bundle_file(target, CODEX_CONFIG_FILE)
            if present
            else None
        )
        failure: ProviderFailure | None = None
        if present and not previous_target:
            failure = _failure(
                ProviderFailureKind.IDENTITY_MISMATCH,
                "The private Codex bundle is not owned by this account.",
            )
        elif previous_target and not present:
            failure = _failure(
                ProviderFailureKind.MISSING,
                "The saved private Codex bundle is missing.",
            )
        elif present and auth is None:
            failure = _failure(
                ProviderFailureKind.MALFORMED,
                "The existing private Codex bundle is incomplete.",
            )
        elif present:
            if previous is None or auth is None:
                raise AssertionError("Owned private bundle has no account.")
            failure = validate_auth_bundle_matches_account(auth, previous)
        return failure or _PrivateTarget(present, auth, config)

    def _previous_owns_target(
        self,
        previous: Account | None,
        target: Path,
    ) -> bool:
        return bool(
            previous is not None
            and previous.codex_home is not None
            and Path(previous.codex_home).expanduser() == target
            and self._private.classify_bundle(target)
            is PrivateCredentialOwnership.CANONICAL
        )

    def _validate_export_target(
        self,
        target: Path,
        source_home: Path | None,
    ) -> ProviderFailure | None:
        if target.name == CODEX_AUTH_FILE:
            return _failure(
                ProviderFailureKind.UNSUPPORTED,
                "A Codex export target must be a dedicated home directory.",
            )
        try:
            target_resolved = target.resolve()
            private_resolved = self._private.root.resolve()
            target_auth = codex_auth_path(target).resolve()
            protected_homes = [
                default_codex_home(),
                *(
                    Path(account.codex_home)
                    for account in self._store
                    if account.codex_home
                ),
            ]
            if source_home is not None:
                protected_homes.append(source_home)
            protected_auth = {
                codex_auth_path(home).resolve() for home in protected_homes
            }
        except OSError, RuntimeError:
            return _failure(
                ProviderFailureKind.UNREADABLE,
                "The requested Codex export path could not be resolved.",
            )
        if (
            target_resolved == private_resolved
            or target_resolved.is_relative_to(private_resolved)
            or target_auth in protected_auth
        ):
            return _failure(
                ProviderFailureKind.UNSUPPORTED,
                "Refusing to export over active, saved, source, or private "
                "Codex credentials.",
            )
        return None

    def _source_homes(
        self,
        account: Account,
        source_home: Path | None,
    ) -> tuple[Path, ...]:
        homes: list[Path] = []
        keys: set[Path] = set()
        candidates = (
            source_home,
            Path(account.codex_home) if account.codex_home else None,
            default_codex_home(),
        )
        for candidate in candidates:
            if candidate is None:
                continue
            home = candidate.expanduser()
            key = codex_auth_path(home).resolve()
            if key not in keys:
                keys.add(key)
                homes.append(home)
        return tuple(homes)

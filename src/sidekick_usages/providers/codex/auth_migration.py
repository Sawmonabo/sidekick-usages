"""Read-only Codex private-auth location migration preparation."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from sidekick_usages.core.models import Account, CodexCredentials
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.persistence.artifacts import Sha256Digest, sha256_digest
from sidekick_usages.persistence.migrations.ports import (
    PreparedPrivateAuthCopy,
    PreparedPrivateAuthMigration,
    PreparedPrivateBundleWrite,
    PrivateAuthAccountAssessment,
    PrivateAuthBundleSnapshot,
    PrivateAuthHomeKind,
    PrivateAuthMigrationAssessment,
    PrivateAuthMigrationFailure,
    PrivateAuthMigrationFailureCode,
    PrivateAuthMigrationRequest,
    PrivateAuthMigrationResult,
)
from sidekick_usages.persistence.private_credentials import (
    portable_private_bundle_path_key,
    private_bundle_relative_components,
    require_portable_unique_private_bundle_paths,
)
from sidekick_usages.providers.base import ProviderFailureKind
from sidekick_usages.providers.codex.auth import (
    CODEX_AUTH_FILE,
    CODEX_CONFIG_FILE,
    CODEX_FILE_AUTH_CONFIG,
    default_codex_home,
    validate_auth_bundle_matches_account,
)

_SOURCE_FILES = frozenset({CODEX_AUTH_FILE, CODEX_CONFIG_FILE})
_RELEASED_PRIVATE_CONFIGS = frozenset(
    f"{CODEX_FILE_AUTH_CONFIG}{ending}".encode()
    for ending in ("", "\n", "\r\n")
)
_SEMANTIC_DIGEST_DOMAIN = b"sidekick-usages:private-auth:v1"
_FRAME_LENGTH_BYTES = 8


@dataclass(frozen=True, slots=True)
class _ClassifiedHome:
    kind: PrivateAuthHomeKind
    root: Path = field(repr=False)
    home: Path = field(repr=False)
    relative_path: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _UnmanagedHome:
    kind: PrivateAuthHomeKind


type _HomeClassification = _ClassifiedHome | _UnmanagedHome


@dataclass(frozen=True, slots=True)
class _SemanticBundle:
    relative_path: str
    files: Mapping[str, bytes] = field(repr=False)


@dataclass(frozen=True, slots=True)
class _PreparedAccount:
    account: Account = field(repr=False)
    assessment: PrivateAuthAccountAssessment
    copy: PreparedPrivateAuthCopy | None = field(repr=False)
    semantic: _SemanticBundle | None = field(repr=False)


class CodexPrivateAuthMigrator:
    """Prepare exact Codex bundle relocation without filesystem mutation."""

    def prepare(
        self,
        request: PrivateAuthMigrationRequest,
    ) -> PrivateAuthMigrationResult:
        """Return deterministic private-auth work or one safe failure."""
        indexed = _index_snapshots(request.bundles)
        if isinstance(indexed, PrivateAuthMigrationFailure):
            return indexed

        prepared_accounts: list[_PreparedAccount] = []
        for account in request.accounts:
            prepared = self._prepare_account(account, request, indexed)
            if isinstance(prepared, PrivateAuthMigrationFailure):
                return prepared
            prepared_accounts.append(prepared)

        semantic: list[_SemanticBundle] = []
        owned_labels: list[AccountLabel] = []
        for prepared in prepared_accounts:
            if prepared.semantic is not None:
                semantic.append(prepared.semantic)
                owned_labels.append(prepared.account.label)
        try:
            require_portable_unique_private_bundle_paths(
                tuple(bundle.relative_path for bundle in semantic)
            )
        except ValueError:
            return _failure(
                PrivateAuthMigrationFailureCode.TARGET_COLLISION,
                "Private Codex bundle targets collide.",
                tuple(owned_labels),
            )

        copies = tuple(
            sorted(
                (
                    prepared.copy
                    for prepared in prepared_accounts
                    if prepared.copy is not None
                ),
                key=lambda copy: portable_private_bundle_path_key(
                    copy.relative_path
                ),
            )
        )
        assessment = PrivateAuthMigrationAssessment(
            tuple(prepared.assessment for prepared in prepared_accounts)
        )
        return PreparedPrivateAuthMigration(
            accounts=tuple(prepared.account for prepared in prepared_accounts),
            assessment=assessment,
            copies=copies,
            semantic_digest=_semantic_digest(tuple(semantic)),
        )

    def _prepare_account(
        self,
        account: Account,
        request: PrivateAuthMigrationRequest,
        snapshots: dict[Path, PrivateAuthBundleSnapshot],
    ) -> _PreparedAccount | PrivateAuthMigrationFailure:
        auth_home = account.codex_home
        if auth_home is None:
            return _PreparedAccount(
                replace(account),
                PrivateAuthAccountAssessment(
                    account.label,
                    PrivateAuthHomeKind.UNSET,
                ),
                None,
                None,
            )
        classification = _classify_home(account, Path(auth_home), request)
        if isinstance(classification, PrivateAuthMigrationFailure):
            return classification
        if isinstance(classification, _UnmanagedHome):
            return _PreparedAccount(
                replace(account),
                PrivateAuthAccountAssessment(
                    account.label,
                    classification.kind,
                ),
                None,
                None,
            )
        source = _validated_source(account, classification, snapshots)
        if isinstance(source, PrivateAuthMigrationFailure):
            return source
        if classification.kind is request.target_kind:
            return _PreparedAccount(
                replace(account),
                PrivateAuthAccountAssessment(
                    account.label,
                    classification.kind,
                ),
                None,
                _SemanticBundle(classification.relative_path, source.files),
            )
        return _prepare_copy(
            account,
            classification,
            source,
            request,
            snapshots,
        )


def _classify_home(
    account: Account,
    home: Path,
    request: PrivateAuthMigrationRequest,
) -> _HomeClassification | PrivateAuthMigrationFailure:
    if not home.is_absolute():
        return _failure(
            PrivateAuthMigrationFailureCode.UNSAFE_HOME,
            "A persisted Codex home is not absolute.",
            (account.label,),
        )
    for kind, root in (
        (request.target_kind, request.target_root),
        (request.source_kind, request.source_root),
    ):
        try:
            relative = home.relative_to(root).as_posix()
        except ValueError:
            continue
        try:
            private_bundle_relative_components(relative)
        except ValueError:
            return _failure(
                PrivateAuthMigrationFailureCode.UNSAFE_HOME,
                "A Sidekick-owned Codex home is not a safe descendant.",
                (account.label,),
            )
        return _ClassifiedHome(kind, root, home, relative)
    if home == default_codex_home():
        return _UnmanagedHome(PrivateAuthHomeKind.PROVIDER_NATIVE)
    return _UnmanagedHome(PrivateAuthHomeKind.EXTERNAL)


def _validated_source(
    account: Account,
    classified: _ClassifiedHome,
    snapshots: dict[Path, PrivateAuthBundleSnapshot],
) -> PrivateAuthBundleSnapshot | PrivateAuthMigrationFailure:
    snapshot = snapshots.get(classified.home)
    if snapshot is None or not snapshot.present:
        return _failure(
            PrivateAuthMigrationFailureCode.SOURCE_MISSING,
            "A referenced Sidekick-owned Codex bundle is missing.",
            (account.label,),
        )
    if not _contained_home(
        classified.home,
        classified.root,
        classified.relative_path,
        require_exists=True,
    ):
        return _failure(
            PrivateAuthMigrationFailureCode.UNSAFE_HOME,
            "A Sidekick-owned Codex bundle escapes its private root.",
            (account.label,),
        )
    if set(snapshot.files) != _SOURCE_FILES or (
        snapshot.files.get(CODEX_CONFIG_FILE) not in _RELEASED_PRIVATE_CONFIGS
    ):
        return _failure(
            PrivateAuthMigrationFailureCode.SOURCE_INVALID,
            "A Sidekick-owned Codex bundle is incomplete or invalid.",
            (account.label,),
        )
    failure = validate_auth_bundle_matches_account(
        snapshot.files[CODEX_AUTH_FILE],
        account,
    )
    if failure is None:
        return snapshot
    if failure.kind is ProviderFailureKind.IDENTITY_MISMATCH:
        return _failure(
            PrivateAuthMigrationFailureCode.SOURCE_IDENTITY_MISMATCH,
            "A Sidekick-owned Codex bundle has another identity.",
            (account.label,),
        )
    return _failure(
        PrivateAuthMigrationFailureCode.SOURCE_INVALID,
        "A Sidekick-owned Codex auth bundle is invalid.",
        (account.label,),
    )


def _prepare_copy(
    account: Account,
    classified: _ClassifiedHome,
    source: PrivateAuthBundleSnapshot,
    request: PrivateAuthMigrationRequest,
    snapshots: dict[Path, PrivateAuthBundleSnapshot],
) -> _PreparedAccount | PrivateAuthMigrationFailure:
    components = private_bundle_relative_components(classified.relative_path)
    target = request.target_root.joinpath(*components)
    observed = snapshots.get(target)
    if observed is None:
        return _failure(
            PrivateAuthMigrationFailureCode.OBSERVATION_CONFLICT,
            "A private Codex target was not assessed.",
            (account.label,),
        )
    if not _contained_home(
        target,
        request.target_root,
        classified.relative_path,
        require_exists=observed.present,
    ):
        return _failure(
            PrivateAuthMigrationFailureCode.UNSAFE_HOME,
            "A private Codex target escapes its Sidekick-owned root.",
            (account.label,),
        )
    if failure := _target_failure(account, source, observed):
        return failure
    credentials = account.credentials
    if not isinstance(credentials, CodexCredentials):
        return _failure(
            PrivateAuthMigrationFailureCode.SOURCE_INVALID,
            "A Codex home belongs to a non-Codex account.",
            (account.label,),
        )
    rewritten = replace(
        account,
        credentials=replace(credentials, auth_home=str(target)),
    )
    copy_required = not observed.present
    assessment = PrivateAuthAccountAssessment(
        account.label,
        classified.kind,
        copy_required,
    )
    copy = None
    if copy_required:
        bundle = PreparedPrivateBundleWrite(
            path=target,
            files=source.files,
            expected_bundle_present=False,
            expected_files={
                CODEX_AUTH_FILE: None,
                CODEX_CONFIG_FILE: None,
            },
        )
        copy = PreparedPrivateAuthCopy(
            account.label,
            classified.relative_path,
            bundle,
        )
    return _PreparedAccount(
        rewritten,
        assessment,
        copy,
        _SemanticBundle(classified.relative_path, source.files),
    )


def _index_snapshots(
    snapshots: tuple[PrivateAuthBundleSnapshot, ...],
) -> dict[Path, PrivateAuthBundleSnapshot] | PrivateAuthMigrationFailure:
    indexed: dict[Path, PrivateAuthBundleSnapshot] = {}
    for snapshot in snapshots:
        if snapshot.home in indexed:
            return _failure(
                PrivateAuthMigrationFailureCode.OBSERVATION_CONFLICT,
                "A private Codex bundle was observed more than once.",
                (),
            )
        indexed[snapshot.home] = snapshot
    return indexed


def _contained_home(
    home: Path,
    root: Path,
    relative: str,
    *,
    require_exists: bool,
) -> bool:
    components = private_bundle_relative_components(relative)
    try:
        resolved_root = root.resolve(strict=require_exists)
        resolved_home = home.resolve(strict=require_exists)
    except OSError, RuntimeError:
        return False
    return resolved_home == resolved_root.joinpath(*components)


def _target_failure(
    account: Account,
    source: PrivateAuthBundleSnapshot,
    target: PrivateAuthBundleSnapshot,
) -> PrivateAuthMigrationFailure | None:
    if not target.present:
        return None
    observed = set(target.files)
    if observed < _SOURCE_FILES:
        return _failure(
            PrivateAuthMigrationFailureCode.TARGET_PARTIAL,
            "A private Codex target is only partially present.",
            (account.label,),
        )
    if observed != _SOURCE_FILES or target.files != source.files:
        return _failure(
            PrivateAuthMigrationFailureCode.TARGET_CONFLICT,
            "A private Codex target conflicts with its source.",
            (account.label,),
        )
    return None


def _semantic_digest(bundles: tuple[_SemanticBundle, ...]) -> Sha256Digest:
    payload = bytearray()
    _append_frame(payload, _SEMANTIC_DIGEST_DOMAIN)
    payload.extend(len(bundles).to_bytes(_FRAME_LENGTH_BYTES, "big"))
    for bundle in sorted(
        bundles,
        key=lambda item: portable_private_bundle_path_key(item.relative_path),
    ):
        _append_frame(payload, bundle.relative_path.encode("utf-8"))
        filenames = tuple(sorted(bundle.files))
        payload.extend(len(filenames).to_bytes(_FRAME_LENGTH_BYTES, "big"))
        for filename in filenames:
            _append_frame(payload, filename.encode("utf-8"))
            _append_frame(payload, bundle.files[filename])
    return sha256_digest(bytes(payload))


def _append_frame(payload: bytearray, value: bytes) -> None:
    payload.extend(len(value).to_bytes(_FRAME_LENGTH_BYTES, "big"))
    payload.extend(value)


def _failure(
    code: PrivateAuthMigrationFailureCode,
    message: str,
    accounts: tuple[AccountLabel, ...],
) -> PrivateAuthMigrationFailure:
    return PrivateAuthMigrationFailure(
        code=code,
        message=message,
        accounts=accounts,
    )


__all__ = ["CodexPrivateAuthMigrator"]

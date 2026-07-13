"""Pure stored-generation and runtime-account transformations."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from sidekick_usages.core.expiry import (
    InvalidExpiry,
    KnownExpiry,
    UnknownExpiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import (
    InvalidSchemaError,
    RollbackCompatibilityError,
)
from sidekick_usages.persistence.migrations.credential_kinds import (
    LegacyClaudeCredentialKind,
    require_migratable_version_one,
)
from sidekick_usages.persistence.schemas import (
    ClaudeCredentialKind,
    GenerationZeroDocument,
    PrototypeDocument,
    StoredAccountRecord,
    StoredClaudeIdentity,
    VersionOneDocument,
    VersionTwoDocument,
    decode_version_one,
    decode_version_two,
    encode_version_one,
    encode_version_two,
)


@dataclass(frozen=True, slots=True)
class _CurrentRecordValues:
    """Provider-owned values normalized for one current record."""

    provider_account_id: str | None
    refresh_token: str | None
    expires_at: datetime | None
    scopes: tuple[str, ...] | None
    codex_home: str | None
    codex_id_token: str | None
    codex_last_refresh: str | None
    credential_kind: ClaudeCredentialKind | None
    refresh_expires_at: datetime | None
    claude_identity: StoredClaudeIdentity | None


def generation_zero_to_version_one(
    document: GenerationZeroDocument,
) -> VersionOneDocument:
    """Transform normalized generation zero to schema version one."""
    return VersionOneDocument(document.accounts)


def prototype_to_version_one(
    document: PrototypeDocument,
) -> VersionOneDocument:
    """Transform an explicit prototype import to schema version one."""
    return VersionOneDocument(
        tuple(
            StoredAccountRecord(
                label=account.label,
                provider_id=ProviderId.CLAUDE,
                provider_account_id=None,
                access_token=account.token,
                refresh_token=None,
                expires_at=None,
                plan=account.plan,
                scopes=None,
                codex_home=None,
                codex_id_token=None,
                codex_last_refresh=None,
                last_refresh_at=None,
                last_refresh_status=None,
                last_refresh_error=None,
                heartbeat_enabled=False,
                heartbeat_5h_reset_at=None,
                heartbeat_window_resets=None,
                heartbeat_targets=None,
                last_heartbeat_at=None,
                last_heartbeat_status=None,
                last_heartbeat_error=None,
            )
            for account in document.accounts
        )
    )


def version_one_to_version_two(
    document: VersionOneDocument,
) -> VersionTwoDocument:
    """Classify and transform complete schema-version-one state."""
    classification = require_migratable_version_one(document)
    kinds = {item.label: item.kind for item in classification.claude_records}
    records: list[StoredAccountRecord] = []
    for record in document.accounts:
        if record.provider_id is ProviderId.CODEX:
            records.append(record)
            continue
        kind = kinds[str(record.label)]
        if kind is LegacyClaudeCredentialKind.SETUP_TOKEN:
            records.append(
                _replace_legacy_claude(
                    record,
                    credential_kind=ClaudeCredentialKind.SETUP_TOKEN,
                    refresh_token=None,
                    expires_at=None,
                    scopes=None,
                )
            )
        elif kind is LegacyClaudeCredentialKind.SUBSCRIPTION_LOGIN:
            records.append(
                _replace_legacy_claude(
                    record,
                    credential_kind=(ClaudeCredentialKind.SUBSCRIPTION_LOGIN),
                    refresh_token=record.refresh_token,
                    expires_at=record.expires_at,
                    scopes=record.scopes,
                )
            )
        else:
            raise AssertionError("Preflight admitted an ambiguous record.")
    result = VersionTwoDocument(tuple(records))
    return decode_version_two(encode_version_two(result))


def _replace_legacy_claude(
    record: StoredAccountRecord,
    *,
    credential_kind: ClaudeCredentialKind,
    refresh_token: str | None,
    expires_at: datetime | None,
    scopes: tuple[str, ...] | None,
) -> StoredAccountRecord:
    """Rebuild one legacy record with explicit Claude variant state."""
    return StoredAccountRecord(
        label=record.label,
        provider_id=record.provider_id,
        provider_account_id=record.provider_account_id,
        access_token=record.access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        plan=record.plan,
        scopes=scopes,
        codex_home=record.codex_home,
        codex_id_token=record.codex_id_token,
        codex_last_refresh=record.codex_last_refresh,
        last_refresh_at=record.last_refresh_at,
        last_refresh_status=record.last_refresh_status,
        last_refresh_error=record.last_refresh_error,
        heartbeat_enabled=record.heartbeat_enabled,
        heartbeat_5h_reset_at=record.heartbeat_5h_reset_at,
        heartbeat_window_resets=record.heartbeat_window_resets,
        heartbeat_targets=record.heartbeat_targets,
        last_heartbeat_at=record.last_heartbeat_at,
        last_heartbeat_status=record.last_heartbeat_status,
        last_heartbeat_error=record.last_heartbeat_error,
        credential_kind=credential_kind,
    )


def generation_zero_to_version_two(
    document: GenerationZeroDocument,
) -> VersionTwoDocument:
    """Deterministically reconstruct current state from released state."""
    return version_one_to_version_two(VersionOneDocument(document.accounts))


def prototype_to_version_two(
    document: PrototypeDocument,
) -> VersionTwoDocument:
    """Transform an explicit prototype import to current state."""
    return version_one_to_version_two(prototype_to_version_one(document))


def version_one_to_v060(
    document: VersionOneDocument,
) -> GenerationZeroDocument:
    """Reverse version one to the complete released v0.6.0 shape."""
    for record in document.accounts:
        if record.heartbeat_targets == () or (
            record.heartbeat_window_resets == ()
        ):
            raise RollbackCompatibilityError
    return GenerationZeroDocument(document.accounts)


def version_two_to_v060(
    document: VersionTwoDocument,
) -> GenerationZeroDocument:
    """Rollback current state, omitting only advisory Claude metadata."""
    validated = decode_version_two(encode_version_two(document))
    records: list[StoredAccountRecord] = []
    for record in validated.accounts:
        if record.provider_id is ProviderId.CODEX:
            records.append(record)
        elif record.credential_kind is ClaudeCredentialKind.SETUP_TOKEN:
            records.append(
                _released_claude_record(
                    record,
                    refresh_token=None,
                    expires_at=None,
                    scopes=("user:inference",),
                )
            )
        elif record.credential_kind is ClaudeCredentialKind.SUBSCRIPTION_LOGIN:
            records.append(
                _released_claude_record(
                    record,
                    refresh_token=record.refresh_token,
                    expires_at=record.expires_at,
                    scopes=record.scopes,
                )
            )
        else:
            raise InvalidSchemaError
    return version_one_to_v060(VersionOneDocument(tuple(records)))


def _released_claude_record(
    record: StoredAccountRecord,
    *,
    refresh_token: str | None,
    expires_at: datetime | None,
    scopes: tuple[str, ...] | None,
) -> StoredAccountRecord:
    """Remove only fields the released schema cannot represent."""
    return StoredAccountRecord(
        label=record.label,
        provider_id=record.provider_id,
        provider_account_id=None,
        access_token=record.access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        plan=record.plan,
        scopes=scopes,
        codex_home=None,
        codex_id_token=None,
        codex_last_refresh=None,
        last_refresh_at=record.last_refresh_at,
        last_refresh_status=record.last_refresh_status,
        last_refresh_error=record.last_refresh_error,
        heartbeat_enabled=record.heartbeat_enabled,
        heartbeat_5h_reset_at=record.heartbeat_5h_reset_at,
        heartbeat_window_resets=record.heartbeat_window_resets,
        heartbeat_targets=record.heartbeat_targets,
        last_heartbeat_at=record.last_heartbeat_at,
        last_heartbeat_status=record.last_heartbeat_status,
        last_heartbeat_error=record.last_heartbeat_error,
    )


def accounts_to_version_one(
    accounts: Iterable[Account],
) -> VersionOneDocument:
    """Convert trusted runtime accounts to validated stored records."""
    records: list[StoredAccountRecord] = []
    for account in accounts:
        credentials = account.credentials
        if isinstance(credentials, ClaudeSetupTokenCredentials):
            provider_account_id = None
            scopes = None
            codex_home = None
            codex_id_token = None
            codex_last_refresh = None
            expiry = UnknownExpiry()
            refresh_token = None
        elif isinstance(credentials, ClaudeLoginCredentials):
            if (
                not isinstance(credentials.refresh_expiry, UnknownExpiry)
                or credentials.identity is not None
            ):
                raise InvalidSchemaError
            provider_account_id = None
            scopes = credentials.scopes
            codex_home = None
            codex_id_token = None
            codex_last_refresh = None
            expiry = credentials.access_expiry
            refresh_token = credentials.refresh_token
        elif isinstance(credentials, CodexCredentials):
            provider_account_id = credentials.account_id
            scopes = None
            codex_home = credentials.auth_home
            codex_id_token = credentials.id_token
            codex_last_refresh = credentials.auth_last_refresh
            expiry = credentials.expiry
            refresh_token = credentials.refresh_token
        else:
            assert_never(credentials)
        if isinstance(expiry, KnownExpiry):
            expires_at = expiry.at
        elif isinstance(expiry, UnknownExpiry):
            expires_at = None
        elif isinstance(expiry, InvalidExpiry):
            raise InvalidSchemaError
        else:
            assert_never(expiry)
        records.append(
            StoredAccountRecord(
                label=account.label,
                provider_id=account.provider_id,
                provider_account_id=provider_account_id,
                access_token=credentials.access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
                plan=account.plan,
                scopes=scopes,
                codex_home=codex_home,
                codex_id_token=codex_id_token,
                codex_last_refresh=codex_last_refresh,
                last_refresh_at=account.last_refresh_at,
                last_refresh_status=account.last_refresh_status,
                last_refresh_error=account.last_refresh_error,
                heartbeat_enabled=account.heartbeat_enabled,
                heartbeat_5h_reset_at=account.heartbeat_5h_reset_at,
                heartbeat_window_resets=(
                    tuple(account.heartbeat_window_resets.items())
                    if account.heartbeat_window_resets is not None
                    else None
                ),
                heartbeat_targets=account.heartbeat_targets,
                last_heartbeat_at=account.last_heartbeat_at,
                last_heartbeat_status=account.last_heartbeat_status,
                last_heartbeat_error=account.last_heartbeat_error,
            )
        )
    document = VersionOneDocument(tuple(records))
    return decode_version_one(encode_version_one(document))


def accounts_to_version_two(
    accounts: Iterable[Account],
) -> VersionTwoDocument:
    """Convert trusted runtime accounts to strict current records."""
    records: list[StoredAccountRecord] = []
    for account in accounts:
        credentials = account.credentials
        if isinstance(credentials, ClaudeSetupTokenCredentials):
            record = _current_account_record(
                account,
                _CurrentRecordValues(
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    ClaudeCredentialKind.SETUP_TOKEN,
                    None,
                    None,
                ),
            )
        elif isinstance(credentials, ClaudeLoginCredentials):
            refresh_expiry = _optional_expiry(credentials.refresh_expiry)
            identity = (
                StoredClaudeIdentity(
                    credentials.identity.account_id,
                    credentials.identity.organization_id,
                )
                if credentials.identity is not None
                else None
            )
            record = _current_account_record(
                account,
                _CurrentRecordValues(
                    None,
                    credentials.refresh_token,
                    credentials.access_expiry.at,
                    credentials.scopes,
                    None,
                    None,
                    None,
                    ClaudeCredentialKind.SUBSCRIPTION_LOGIN,
                    refresh_expiry,
                    identity,
                ),
            )
        elif isinstance(credentials, CodexCredentials):
            record = _current_account_record(
                account,
                _CurrentRecordValues(
                    credentials.account_id,
                    credentials.refresh_token,
                    _optional_expiry(credentials.expiry),
                    None,
                    credentials.auth_home,
                    credentials.id_token,
                    credentials.auth_last_refresh,
                    None,
                    None,
                    None,
                ),
            )
        else:
            assert_never(credentials)
        records.append(record)
    document = VersionTwoDocument(tuple(records))
    return decode_version_two(encode_version_two(document))


def _optional_expiry(
    expiry: KnownExpiry | UnknownExpiry | InvalidExpiry,
) -> datetime | None:
    if isinstance(expiry, KnownExpiry):
        return expiry.at
    if isinstance(expiry, UnknownExpiry):
        return None
    if isinstance(expiry, InvalidExpiry):
        raise InvalidSchemaError
    assert_never(expiry)


def _current_account_record(
    account: Account,
    values: _CurrentRecordValues,
) -> StoredAccountRecord:
    credentials = account.credentials
    return StoredAccountRecord(
        label=account.label,
        provider_id=account.provider_id,
        provider_account_id=values.provider_account_id,
        access_token=credentials.access_token,
        refresh_token=values.refresh_token,
        expires_at=values.expires_at,
        plan=account.plan,
        scopes=values.scopes,
        codex_home=values.codex_home,
        codex_id_token=values.codex_id_token,
        codex_last_refresh=values.codex_last_refresh,
        last_refresh_at=account.last_refresh_at,
        last_refresh_status=account.last_refresh_status,
        last_refresh_error=account.last_refresh_error,
        heartbeat_enabled=account.heartbeat_enabled,
        heartbeat_5h_reset_at=account.heartbeat_5h_reset_at,
        heartbeat_window_resets=(
            tuple(account.heartbeat_window_resets.items())
            if account.heartbeat_window_resets is not None
            else None
        ),
        heartbeat_targets=account.heartbeat_targets,
        last_heartbeat_at=account.last_heartbeat_at,
        last_heartbeat_status=account.last_heartbeat_status,
        last_heartbeat_error=account.last_heartbeat_error,
        credential_kind=values.credential_kind,
        refresh_expires_at=values.refresh_expires_at,
        claude_identity=values.claude_identity,
    )


def version_two_to_accounts(
    document: VersionTwoDocument,
) -> tuple[Account, ...]:
    """Convert strict current records to closed runtime accounts."""
    validated = decode_version_two(encode_version_two(document))
    accounts: list[Account] = []
    for record in validated.accounts:
        if record.provider_id is ProviderId.CLAUDE:
            if record.credential_kind is ClaudeCredentialKind.SETUP_TOKEN:
                credentials = ClaudeSetupTokenCredentials(
                    access_token=record.access_token
                )
            elif (
                record.credential_kind
                is ClaudeCredentialKind.SUBSCRIPTION_LOGIN
                and record.refresh_token is not None
                and record.expires_at is not None
                and record.scopes is not None
            ):
                identity = (
                    ClaudeLoginIdentity(
                        account_id=record.claude_identity.account_id,
                        organization_id=(
                            record.claude_identity.organization_id
                        ),
                    )
                    if record.claude_identity is not None
                    else None
                )
                credentials = ClaudeLoginCredentials(
                    access_token=record.access_token,
                    refresh_token=record.refresh_token,
                    access_expiry=KnownExpiry(record.expires_at),
                    refresh_expiry=(
                        KnownExpiry(record.refresh_expires_at)
                        if record.refresh_expires_at is not None
                        else UnknownExpiry()
                    ),
                    scopes=record.scopes,
                    identity=identity,
                )
            else:
                raise InvalidSchemaError
        else:
            credentials = CodexCredentials(
                access_token=record.access_token,
                refresh_token=record.refresh_token,
                expiry=(
                    KnownExpiry(record.expires_at)
                    if record.expires_at is not None
                    else UnknownExpiry()
                ),
                account_id=record.provider_account_id,
                auth_home=record.codex_home,
                id_token=record.codex_id_token,
                auth_last_refresh=record.codex_last_refresh,
            )
        accounts.append(_runtime_account(record, credentials))
    return tuple(accounts)


def _runtime_account(
    record: StoredAccountRecord,
    credentials: (
        ClaudeSetupTokenCredentials | ClaudeLoginCredentials | CodexCredentials
    ),
) -> Account:
    return Account(
        label=record.label,
        credentials=credentials,
        plan=record.plan,
        last_refresh_at=record.last_refresh_at,
        last_refresh_status=record.last_refresh_status,
        last_refresh_error=record.last_refresh_error,
        heartbeat_enabled=record.heartbeat_enabled,
        heartbeat_5h_reset_at=record.heartbeat_5h_reset_at,
        heartbeat_window_resets=(
            dict(record.heartbeat_window_resets)
            if record.heartbeat_window_resets is not None
            else None
        ),
        heartbeat_targets=record.heartbeat_targets,
        last_heartbeat_at=record.last_heartbeat_at,
        last_heartbeat_status=record.last_heartbeat_status,
        last_heartbeat_error=record.last_heartbeat_error,
    )


def version_one_to_accounts(
    document: VersionOneDocument,
) -> tuple[Account, ...]:
    """Convert validated stored records to runtime accounts."""
    validated = decode_version_one(encode_version_one(document))
    accounts: list[Account] = []
    for record in validated.accounts:
        expiry = (
            KnownExpiry(record.expires_at)
            if record.expires_at is not None
            else UnknownExpiry()
        )
        if record.provider_id is ProviderId.CLAUDE:
            has_refresh_token = record.refresh_token is not None
            has_access_expiry = record.expires_at is not None
            has_profile_scope = (
                record.scopes is not None and "user:profile" in record.scopes
            )
            if has_refresh_token and has_access_expiry and has_profile_scope:
                assert record.refresh_token is not None
                assert isinstance(expiry, KnownExpiry)
                assert record.scopes is not None
                credentials = ClaudeLoginCredentials(
                    access_token=record.access_token,
                    refresh_token=record.refresh_token,
                    access_expiry=expiry,
                    refresh_expiry=UnknownExpiry(),
                    scopes=record.scopes,
                    identity=None,
                )
            elif not (
                has_refresh_token or has_access_expiry or has_profile_scope
            ):
                credentials = ClaudeSetupTokenCredentials(
                    access_token=record.access_token
                )
            else:
                raise InvalidSchemaError
        else:
            credentials = CodexCredentials(
                access_token=record.access_token,
                refresh_token=record.refresh_token,
                expiry=expiry,
                account_id=record.provider_account_id,
                auth_home=record.codex_home,
                id_token=record.codex_id_token,
                auth_last_refresh=record.codex_last_refresh,
            )
        accounts.append(
            Account(
                label=record.label,
                credentials=credentials,
                plan=record.plan,
                last_refresh_at=record.last_refresh_at,
                last_refresh_status=record.last_refresh_status,
                last_refresh_error=record.last_refresh_error,
                heartbeat_enabled=record.heartbeat_enabled,
                heartbeat_5h_reset_at=record.heartbeat_5h_reset_at,
                heartbeat_window_resets=(
                    dict(record.heartbeat_window_resets)
                    if record.heartbeat_window_resets is not None
                    else None
                ),
                heartbeat_targets=record.heartbeat_targets,
                last_heartbeat_at=record.last_heartbeat_at,
                last_heartbeat_status=record.last_heartbeat_status,
                last_heartbeat_error=record.last_heartbeat_error,
            )
        )
    return tuple(accounts)


__all__ = [
    "accounts_to_version_one",
    "accounts_to_version_two",
    "generation_zero_to_version_one",
    "generation_zero_to_version_two",
    "prototype_to_version_one",
    "prototype_to_version_two",
    "version_one_to_accounts",
    "version_one_to_v060",
    "version_one_to_version_two",
    "version_two_to_accounts",
    "version_two_to_v060",
]

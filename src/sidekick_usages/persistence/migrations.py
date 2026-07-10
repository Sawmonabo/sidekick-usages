"""Pure stored-generation and runtime-account transformations."""

from collections.abc import Iterable
from typing import assert_never

from sidekick_usages.core.expiry import (
    InvalidExpiry,
    KnownExpiry,
    UnknownExpiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.persistence.errors import (
    InvalidSchemaError,
    RollbackCompatibilityError,
)
from sidekick_usages.persistence.schemas import (
    GenerationZeroDocument,
    PrototypeDocument,
    StoredAccountRecord,
    VersionOneDocument,
    decode_version_one,
    encode_version_one,
)


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


def accounts_to_version_one(
    accounts: Iterable[Account],
) -> VersionOneDocument:
    """Convert trusted runtime accounts to validated stored records."""
    records: list[StoredAccountRecord] = []
    for account in accounts:
        credentials = account.credentials
        if isinstance(credentials, ClaudeCredentials):
            provider_account_id = None
            scopes = credentials.scopes
            codex_home = None
            codex_id_token = None
            codex_last_refresh = None
        else:
            provider_account_id = credentials.account_id
            scopes = None
            codex_home = credentials.auth_home
            codex_id_token = credentials.id_token
            codex_last_refresh = credentials.auth_last_refresh
        expiry = credentials.expiry
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
                refresh_token=credentials.refresh_token,
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
            credentials = ClaudeCredentials(
                access_token=record.access_token,
                refresh_token=record.refresh_token,
                expiry=expiry,
                scopes=record.scopes,
            )
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

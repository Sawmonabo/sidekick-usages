"""Compatibility account codec and account-store CRUD."""

import contextlib
import json
import os
import platform
import stat
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

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
from sidekick_usages.core.types import (
    AccountLabel,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.errors import InvalidPayloadError, UsageError
from sidekick_usages.paths import AccountLocations
from sidekick_usages.serialization import (
    JsonObject,
    JsonValue,
    decode_json_object,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECONDS_PER_SECOND = 1_000_000
_MICROSECONDS_PER_MILLISECOND = 1_000


def _invalid_account(label: str, detail: str) -> UsageError:
    """Build a redacted compatibility-state failure."""
    return UsageError(f"Account {label!r} is invalid: {detail}.")


def _validated_label(value: str) -> AccountLabel:
    """Validate a label at the account-store boundary."""
    try:
        return AccountLabel(value)
    except ValueError as error:
        raise _invalid_account(value, str(error).removesuffix(".")) from error


def _optional_string(
    data: JsonObject,
    key: str,
    label: str,
) -> str | None:
    value = data.get(key)
    if value is None or isinstance(value, str):
        return value
    raise _invalid_account(label, f"{key} must be a string or null")


def _required_string(data: JsonObject, key: str, label: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    raise _invalid_account(label, f"{key} must be a non-empty string")


def _optional_strings(
    data: JsonObject,
    key: str,
    label: str,
) -> tuple[str, ...] | None:
    value = data.get(key)
    if value is None:
        return None
    if isinstance(value, list) and all(
        isinstance(item, str) for item in value
    ):
        return tuple(item for item in value if isinstance(item, str))
    raise _invalid_account(label, f"{key} must be a string list or null")


def _optional_time(
    data: JsonObject,
    key: str,
    label: str,
) -> datetime | None:
    value = _optional_string(data, key, label)
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise _invalid_account(
            label, f"{key} must be an ISO timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _invalid_account(label, f"{key} must be timezone-aware")
    return parsed.astimezone(UTC)


def _optional_time_map(
    data: JsonObject,
    key: str,
    label: str,
) -> dict[str, datetime] | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _invalid_account(label, f"{key} must be an object or null")
    result: dict[str, datetime] = {}
    for target_id, item in value.items():
        if not isinstance(item, str):
            raise _invalid_account(label, f"{key} values must be timestamps")
        try:
            parsed = datetime.fromisoformat(item.replace("Z", "+00:00"))
        except ValueError as error:
            raise _invalid_account(
                label, f"{key} values must be timestamps"
            ) from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise _invalid_account(label, f"{key} values must be aware")
        result[target_id] = parsed.astimezone(UTC)
    return result


def _native_expiry(
    provider_id: ProviderId,
    value: JsonValue | None,
    label: str,
) -> KnownExpiry | UnknownExpiry:
    if value is None:
        return UnknownExpiry()
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid_account(
            label, "expires_at must be a non-negative integer"
        )
    try:
        delta = (
            timedelta(milliseconds=value)
            if provider_id is ProviderId.CLAUDE
            else timedelta(seconds=value)
        )
        return KnownExpiry(_EPOCH + delta)
    except OverflowError as error:
        raise _invalid_account(label, "expires_at is out of range") from error


def _encode_native_expiry(account: Account) -> int | None:
    expiry = account.expiry
    if isinstance(expiry, UnknownExpiry):
        return None
    if isinstance(expiry, InvalidExpiry):
        raise _invalid_account(
            str(account.label), "expiry cannot be serialized"
        )
    delta = expiry.at - _EPOCH
    microseconds = (
        delta.days * 86_400 + delta.seconds
    ) * _MICROSECONDS_PER_SECOND + delta.microseconds
    unit = (
        _MICROSECONDS_PER_MILLISECOND
        if account.provider_id is ProviderId.CLAUDE
        else _MICROSECONDS_PER_SECOND
    )
    if microseconds < 0 or microseconds % unit:
        raise _invalid_account(
            str(account.label),
            "expiry precision is not representable by its provider",
        )
    return microseconds // unit


def _format_time(value: datetime | None) -> str | None:
    """Encode a Sidekick-owned compatibility timestamp."""
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise UsageError("Cannot serialize a naive account timestamp.")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _account_from_record(label_value: str, data: JsonObject) -> Account:
    """Decode one generation-zero or prototype account record."""
    label = _validated_label(label_value)
    if "access_token" not in data:
        credentials = ClaudeCredentials(
            access_token=_required_string(data, "token", label_value),
        )
        return Account(
            label=label,
            credentials=credentials,
            plan=_required_string(data, "plan", label_value),
        )

    provider_value = data.get("provider_id", ProviderId.CLAUDE.value)
    if not isinstance(provider_value, str):
        raise _invalid_account(label_value, "provider_id must be a string")
    try:
        provider_id = ProviderId(provider_value)
    except ValueError as error:
        raise _invalid_account(
            label_value, "provider_id is unsupported"
        ) from error

    access_token = _required_string(data, "access_token", label_value)
    refresh_token = _optional_string(data, "refresh_token", label_value)
    expiry = _native_expiry(provider_id, data.get("expires_at"), label_value)
    if provider_id is ProviderId.CLAUDE:
        for key in (
            "provider_account_id",
            "codex_home",
            "codex_id_token",
            "codex_last_refresh",
        ):
            if data.get(key) is not None:
                raise _invalid_account(
                    label_value,
                    f"{key} is incompatible with Claude",
                )
        credentials = ClaudeCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            expiry=expiry,
            scopes=_optional_strings(data, "scopes", label_value),
        )
    else:
        if data.get("scopes") is not None:
            raise _invalid_account(
                label_value, "scopes is incompatible with Codex"
            )
        credentials = CodexCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            expiry=expiry,
            account_id=_optional_string(
                data, "provider_account_id", label_value
            ),
            auth_home=_optional_string(data, "codex_home", label_value),
            id_token=_optional_string(data, "codex_id_token", label_value),
            auth_last_refresh=_optional_string(
                data,
                "codex_last_refresh",
                label_value,
            ),
        )

    refresh_status_value = _optional_string(
        data,
        "last_refresh_status",
        label_value,
    )
    heartbeat_status_value = _optional_string(
        data,
        "last_heartbeat_status",
        label_value,
    )
    heartbeat_enabled = data.get("heartbeat_enabled", False)
    if not isinstance(heartbeat_enabled, bool):
        raise _invalid_account(
            label_value, "heartbeat_enabled must be Boolean"
        )
    try:
        refresh_status = (
            RefreshStatus(refresh_status_value)
            if refresh_status_value is not None
            else None
        )
        heartbeat_status = (
            HeartbeatStatus(heartbeat_status_value)
            if heartbeat_status_value is not None
            else None
        )
    except ValueError as error:
        raise _invalid_account(
            label_value, "stored status is unsupported"
        ) from error

    return Account(
        label=label,
        credentials=credentials,
        plan=_required_string(data, "plan", label_value),
        last_refresh_at=_optional_time(data, "last_refresh_at", label_value),
        last_refresh_status=refresh_status,
        last_refresh_error=_optional_string(
            data,
            "last_refresh_error",
            label_value,
        ),
        heartbeat_enabled=heartbeat_enabled,
        heartbeat_5h_reset_at=_optional_time(
            data,
            "heartbeat_5h_reset_at",
            label_value,
        ),
        heartbeat_window_resets=_optional_time_map(
            data,
            "heartbeat_window_resets",
            label_value,
        ),
        heartbeat_targets=_optional_strings(
            data,
            "heartbeat_targets",
            label_value,
        ),
        last_heartbeat_at=_optional_time(
            data,
            "last_heartbeat_at",
            label_value,
        ),
        last_heartbeat_status=heartbeat_status,
        last_heartbeat_error=_optional_string(
            data,
            "last_heartbeat_error",
            label_value,
        ),
    )


def _account_to_record(account: Account) -> JsonObject:
    """Encode one account in the released generation-zero field order."""
    credentials = account.credentials
    provider_account_id: str | None = None
    scopes: list[JsonValue] | None = None
    codex_home: str | None = None
    codex_id_token: str | None = None
    codex_last_refresh: str | None = None
    if isinstance(credentials, ClaudeCredentials):
        if credentials.scopes is not None:
            scopes = list(credentials.scopes)
    else:
        provider_account_id = credentials.account_id
        codex_home = credentials.auth_home
        codex_id_token = credentials.id_token
        codex_last_refresh = credentials.auth_last_refresh
    resets: dict[str, JsonValue] | None = None
    if account.heartbeat_window_resets is not None:
        resets = {
            target_id: _format_time(reset_at)
            for target_id, reset_at in account.heartbeat_window_resets.items()
        }
    return {
        "provider_id": account.provider_id.value,
        "provider_account_id": provider_account_id,
        "access_token": account.access_token,
        "refresh_token": account.refresh_token,
        "expires_at": _encode_native_expiry(account),
        "plan": account.plan,
        "scopes": scopes,
        "codex_home": codex_home,
        "codex_id_token": codex_id_token,
        "codex_last_refresh": codex_last_refresh,
        "last_refresh_at": _format_time(account.last_refresh_at),
        "last_refresh_status": (
            account.last_refresh_status.value
            if account.last_refresh_status is not None
            else None
        ),
        "last_refresh_error": account.last_refresh_error,
        "heartbeat_enabled": account.heartbeat_enabled,
        "heartbeat_5h_reset_at": _format_time(account.heartbeat_5h_reset_at),
        "heartbeat_window_resets": resets,
        "heartbeat_targets": (
            list(account.heartbeat_targets)
            if account.heartbeat_targets is not None
            else None
        ),
        "last_heartbeat_at": _format_time(account.last_heartbeat_at),
        "last_heartbeat_status": (
            account.last_heartbeat_status.value
            if account.last_heartbeat_status is not None
            else None
        ),
        "last_heartbeat_error": account.last_heartbeat_error,
    }


class AccountStore:
    """Loads, saves, and CRUDs accounts in the JSON config file.

    Order of insertion is preserved on save via dict insertion order
    (guaranteed in Python 3.7+).
    """

    def __init__(self, locations: AccountLocations) -> None:
        """:param locations: Account-store compatibility locations."""
        self.locations = locations
        self.path = locations.canonical
        self._accounts: dict[str, Account] = {}
        self._loaded = False

    # -- persistence ------------------------------------------------
    def load(self) -> AccountStore:
        """Read accounts from disk if not already loaded.

        If the selected account file does not exist but the configured
        cc-usage prototype file does, migrate its contents into the
        selected path automatically. The prototype file is left in
        place — we never delete user data.

        :return: ``self`` (for chaining).
        """
        if self._loaded:
            return self
        prototype_path = self.locations.prototype_cc_usage
        if not self.path.exists() and prototype_path.exists():
            self._migrate_from_prototype()
        if not self.path.exists():
            self._loaded = True
            return self
        try:
            raw = decode_json_object(self.path.read_bytes())
        except InvalidPayloadError as error:
            raise UsageError(f"Config file is corrupt: {self.path}") from error
        accounts: dict[str, Account] = {}
        for label, value in raw.items():
            if not isinstance(value, dict):
                raise _invalid_account(label, "record must be an object")
            accounts[label] = _account_from_record(label, value)
        self._accounts = accounts
        self._loaded = True
        return self

    def save(self) -> None:
        """Persist current state to disk with 600 perms on Unix."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            label: _account_to_record(acct)
            for label, acct in self._accounts.items()
        }
        self.path.write_text(json.dumps(payload, indent=2))
        if platform.system() != "Windows":
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    def _migrate_from_prototype(self) -> None:
        """Copy the prototype cc-usage config into the current location."""
        try:
            raw = decode_json_object(
                self.locations.prototype_cc_usage.read_bytes()
            )
        except (OSError, InvalidPayloadError) as error:
            raise UsageError("Prototype account file is invalid.") from error
        migrated: dict[str, JsonObject] = {}
        for label, value in raw.items():
            if not isinstance(value, dict):
                raise _invalid_account(label, "record must be an object")
            migrated[label] = _account_to_record(
                _account_from_record(label, value)
            )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(migrated, indent=2))
        if platform.system() != "Windows":
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    # -- queries ----------------------------------------------------
    def __iter__(self) -> Iterator[Account]:
        return iter(self._accounts.values())

    def __len__(self) -> int:
        return len(self._accounts)

    def __contains__(self, label: object) -> bool:
        return label in self._accounts

    def get(self, label: str) -> Account | None:
        """Look up an account by label.

        :param label: Account label.
        :return: ``Account`` or ``None`` if not found.
        """
        return self._accounts.get(label)

    def find_by_token(
        self,
        provider_id: ProviderId,
        token: str,
    ) -> Account | None:
        """Look up an account by exact access-token match.

        Used by ``add`` to make the operation idempotent.

        :param provider_id: Provider whose token namespace to search.
        :param token: OAuth access token to search for.
        :return: ``Account`` or ``None``.
        """
        for acct in self._accounts.values():
            if acct.provider_id is provider_id and acct.access_token == token:
                return acct
        return None

    def filter_by_provider(self, provider_id: ProviderId) -> list[Account]:
        """Return accounts for one provider in insertion order.

        :param provider_id: Provider id (``"claude"`` or ``"codex"``).
        :return: List of matching accounts.
        """
        return [
            a for a in self._accounts.values() if a.provider_id == provider_id
        ]

    # -- mutations --------------------------------------------------
    def upsert(self, account: Account) -> None:
        """Insert or replace an account in-place by label.

        :param account: Account to store.
        """
        self._accounts[account.label] = account

    def remove(self, label: str) -> bool:
        """Delete an account by label.

        :param label: Account label.
        :return: True if deleted, False if the label was unknown.
        """
        if label not in self._accounts:
            return False
        del self._accounts[label]
        return True

    def rename(self, old: str, new: str) -> bool:
        """Rename an account, preserving insertion order.

        :param old: Existing label.
        :param new: New label (must not already exist unless equal).
        :return: True on success, False if ``old`` is unknown or
            ``new`` collides with a different existing label.
        """
        if old not in self._accounts:
            return False
        new_label = _validated_label(new)
        if new_label in self._accounts and new_label != old:
            return False
        new_map: dict[str, Account] = {}
        for label, acct in self._accounts.items():
            if label == old:
                acct.label = new_label
                new_map[new_label] = acct
            else:
                new_map[label] = acct
        self._accounts = new_map
        return True

    def reset(self) -> int:
        """Drop every saved account and remove the on-disk file.

        :return: Number of accounts that were cleared.
        """
        count = len(self._accounts)
        self._accounts.clear()
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        return count

    def reset_provider(self, provider_id: ProviderId) -> int:
        """Drop accounts for one provider, keep the rest.

        :param provider_id: Provider id to clear.
        :return: Number of accounts that were removed.
        """
        targets = [
            label
            for label, a in self._accounts.items()
            if a.provider_id == provider_id
        ]
        for label in targets:
            del self._accounts[label]
        if self._accounts:
            self.save()
        else:
            self.reset()
        return len(targets)

    def generate_label(
        self,
        provider_id: ProviderId,
        plan: str,
    ) -> AccountLabel:
        """Build a unique default label from provider + plan.

        ``claude`` + ``max`` -> ``claude-max-1``, then ``-2``, etc.

        :param provider_id: Provider id.
        :param plan: Subscription type tag.
        :return: Smallest unused label.
        """
        plan_clean = (plan or "account").lower().replace(" ", "-")
        base = f"{provider_id}-{plan_clean}"
        i = 1
        while f"{base}-{i}" in self._accounts:
            i += 1
        return _validated_label(f"{base}-{i}")

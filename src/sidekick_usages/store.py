"""Account model and AccountStore CRUD.

Accounts now carry a ``provider_id`` field so a single store can hold
Claude Code and Codex CLI accounts side by side. The on-disk format
is JSON keyed by label, with provider id and other fields inside.
"""

import contextlib
import json
import os
import platform
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sidekick_usages.errors import UsageError

CONFIG_DIR = Path.home() / ".config" / "sidekick-usages"
CONFIG_FILE = CONFIG_DIR / "accounts.json"

# Legacy path from the cc-usage.py prototype. We auto-migrate the
# first time the new store is loaded so users keep their data.
LEGACY_CONFIG_FILE = Path.home() / ".config" / "cc-usage" / "accounts.json"

#: Minimum token length required to render a partial mask
#: (``prefix…suffix``). Below this we render ``"(missing)"`` because
#: a mask would either reveal too much of a short token or be
#: indistinguishable from the original.
_MIN_TOKEN_LENGTH_FOR_MASKING = 30


@dataclass
class Account:
    """A single saved AI assistant account.

    :ivar label: Friendly user-visible name (e.g. ``personal-max``).
    :ivar provider_id: Which provider this belongs to
        (``"claude"`` or ``"codex"``).
    :ivar provider_account_id: Provider-native account/workspace id
        needed by APIs that bind requests to an account separate from
        the bearer token. Codex uses this as ``ChatGPT-Account-Id``.
    :ivar access_token: OAuth access token used as the Bearer auth.
    :ivar refresh_token: Refresh token for providers/account types
        that expose one. ``None`` for pasted or long-lived tokens
        without refresh support.
    :ivar expires_at: Provider-native Unix timestamp when
        ``access_token`` expires (Claude uses milliseconds; Codex uses
        seconds). ``None`` if unknown.
    :ivar plan: Subscription type tag (``"max"``, ``"plus"``, etc.).
    :ivar scopes: OAuth scope list when known (read from the local
        credentials file at detect-time). ``None`` means the scopes
        are unknown — typical when the token was pasted via
        ``--token`` instead of auto-detected. Drives the
        client-side gate that mirrors Claude Code's ``hT()`` check:
        when scopes are known and do not include
        ``user:profile``, the usage endpoint is skipped because the
        token cannot read it anyway.
    :ivar codex_home: Sidekick-owned per-account Codex auth cache
        directory. This is not the user's default ``~/.codex`` root;
        it is an internal copy used so sidekick-usages can query
        multiple Codex accounts without changing global Codex state.
    :ivar codex_id_token: Codex ``auth.json`` id token. The Codex CLI
        needs this alongside the access/refresh tokens when we write
        a complete file-backed auth store.
    :ivar codex_last_refresh: Last refresh timestamp from Codex
        ``auth.json``. Preserved when known so exported auth files
        stay close to the CLI's native shape.
    :ivar last_refresh_at: ISO-8601 UTC timestamp for the last
        refresh attempt sidekick-usages made for this account.
    :ivar last_refresh_status: Result tag for the last refresh
        attempt (``"ok"``, ``"skipped"``, or ``"failed"``).
    :ivar last_refresh_error: Redacted human-readable error from the
        last failed refresh attempt, or ``None`` after a success.
    """

    label: str
    provider_id: str
    access_token: str
    provider_account_id: str | None = None
    refresh_token: str | None = None
    expires_at: int | None = None
    plan: str = "unknown"
    scopes: list[str] | None = None
    codex_home: str | None = None
    codex_id_token: str | None = None
    codex_last_refresh: str | None = None
    last_refresh_at: str | None = None
    last_refresh_status: str | None = None
    last_refresh_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON storage.

        :return: Plain dict; label is the parent key so it isn't
            duplicated inside.
        """
        return {
            "provider_id": self.provider_id,
            "provider_account_id": self.provider_account_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "plan": self.plan,
            "scopes": self.scopes,
            "codex_home": self.codex_home,
            "codex_id_token": self.codex_id_token,
            "codex_last_refresh": self.codex_last_refresh,
            "last_refresh_at": self.last_refresh_at,
            "last_refresh_status": self.last_refresh_status,
            "last_refresh_error": self.last_refresh_error,
        }

    @classmethod
    def from_dict(
        cls,
        label: str,
        data: dict[str, Any],
    ) -> Account:
        """Reverse of :meth:`to_dict`.

        Accepts both the new schema and the legacy ``cc-usage`` schema
        (``{"token": ..., "plan": ...}``) for migration.

        :param label: Label this account is stored under.
        :param data: Dict read from the config file.
        :return: Reconstructed ``Account``.
        """
        if "access_token" in data:
            return cls(
                label=label,
                provider_id=data.get("provider_id", "claude"),
                provider_account_id=data.get("provider_account_id"),
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token"),
                expires_at=data.get("expires_at"),
                plan=data.get("plan", "unknown"),
                scopes=data.get("scopes"),
                codex_home=data.get("codex_home"),
                codex_id_token=data.get("codex_id_token"),
                codex_last_refresh=data.get("codex_last_refresh"),
                last_refresh_at=data.get("last_refresh_at"),
                last_refresh_status=data.get("last_refresh_status"),
                last_refresh_error=data.get("last_refresh_error"),
            )
        # Legacy cc-usage.py format: {"token": ..., "plan": ...}
        return cls(
            label=label,
            provider_id="claude",
            access_token=data.get("token", ""),
            plan=data.get("plan", "unknown"),
        )

    def masked_token(self) -> str:
        """Render the token with the middle redacted for display.

        :return: Something like ``sk-ant-oat01-abcd…xyz123`` or
            ``(missing)`` if no token is set.
        """
        if len(self.access_token) <= _MIN_TOKEN_LENGTH_FOR_MASKING:
            return "(missing)"
        return self.access_token[:18] + "…" + self.access_token[-6:]


class AccountStore:
    """Loads, saves, and CRUDs accounts in the JSON config file.

    Order of insertion is preserved on save via dict insertion order
    (guaranteed in Python 3.7+).
    """

    def __init__(self, path: Path = CONFIG_FILE) -> None:
        """:param path: Path to the JSON config file."""
        self.path = path
        self._accounts: dict[str, Account] = {}
        self._loaded = False

    # -- persistence ------------------------------------------------
    def load(self) -> AccountStore:
        """Read accounts from disk if not already loaded.

        If the new config file does not exist but the legacy
        ``~/.config/cc-usage/accounts.json`` does, migrate its
        contents into the new path automatically. The legacy file is
        left in place — we never delete user data.

        :return: ``self`` (for chaining).
        """
        if self._loaded:
            return self
        if not self.path.exists() and LEGACY_CONFIG_FILE.exists():
            self._migrate_from_legacy()
        if not self.path.exists():
            self._loaded = True
            return self
        try:
            raw = json.loads(self.path.read_text())
        except json.JSONDecodeError as e:
            raise UsageError(
                f"Config file is corrupt: {self.path} ({e})"
            ) from e
        self._accounts = {
            label: Account.from_dict(label, data)
            for label, data in raw.items()
        }
        self._loaded = True
        return self

    def save(self) -> None:
        """Persist current state to disk with 600 perms on Unix.

        :return: None.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            label: acct.to_dict() for label, acct in self._accounts.items()
        }
        self.path.write_text(json.dumps(payload, indent=2))
        if platform.system() != "Windows":
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    def _migrate_from_legacy(self) -> None:
        """Copy legacy cc-usage config into the new location.

        :return: None.
        """
        try:
            raw = json.loads(LEGACY_CONFIG_FILE.read_text())
        except json.JSONDecodeError, OSError:
            return
        migrated = {
            label: Account.from_dict(label, data).to_dict()
            for label, data in raw.items()
        }
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

    def find_by_token(self, token: str) -> Account | None:
        """Look up an account by exact access-token match.

        Used by ``add`` to make the operation idempotent.

        :param token: OAuth access token to search for.
        :return: ``Account`` or ``None``.
        """
        for acct in self._accounts.values():
            if acct.access_token == token:
                return acct
        return None

    def filter_by_provider(self, provider_id: str) -> list[Account]:
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
        :return: None.
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
        if new in self._accounts and new != old:
            return False
        new_map: dict[str, Account] = {}
        for label, acct in self._accounts.items():
            if label == old:
                acct.label = new
                new_map[new] = acct
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

    def reset_provider(self, provider_id: str) -> int:
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

    def generate_label(self, provider_id: str, plan: str) -> str:
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
        return f"{base}-{i}"

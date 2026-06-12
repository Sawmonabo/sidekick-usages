"""Codex CLI provider.

Reads OAuth credentials from ``~/.codex/auth.json`` or an explicit
per-account ``CODEX_HOME`` auth store. Calls
``https://chatgpt.com/backend-api/codex/usage`` and parses the
``primary_window`` (5h), ``secondary_window`` (7d), and per-model
``additional_rate_limits`` buckets.

Codex access tokens expire roughly hourly, so :meth:`refresh_token`
exchanges the stored refresh_token against
``https://auth.openai.com/oauth/token``. The CLI calls this
automatically when usage requests return 401.

Codex has no analogue to ``claude setup-token``, so
:meth:`run_setup_token` raises :class:`UnsupportedOperationError`.
"""

import json
import os
import platform
import re
import stat
import time
from base64 import urlsafe_b64decode
from binascii import Error as B64Error
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sidekick_usages.errors import (
    AuthError,
    UnsupportedOperationError,
    UsageError,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.providers.base import (
    DetectedCredentials,
    Provider,
)
from sidekick_usages.report import UsageReport, UsageWindow
from sidekick_usages.store import Account

USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
OAUTH_REFRESH_ENDPOINT = "https://auth.openai.com/oauth/token"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
USER_AGENT = "codex-cli/0.139.0"
JWT_MIN_PARTS = 2
CODEX_HOME_ENV = "CODEX_HOME"
CODEX_AUTH_FILE = "auth.json"
CODEX_CONFIG_FILE = "config.toml"
CODEX_FILE_AUTH_CONFIG = 'cli_auth_credentials_store = "file"'

# Codex tokens are opaque JWTs. We don't pattern-match them tightly;
# we just look for something that starts with "eyJ" (JWT header) and
# is reasonably long. Looser than Claude's prefix-based shape check.
TOKEN_RE = re.compile(
    r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\."
    r"[A-Za-z0-9_\-]+"
)


class CodexProvider(Provider):
    """Codex CLI integration."""

    id = "codex"
    display_name = "Codex CLI"
    token_pattern = TOKEN_RE

    def __init__(self) -> None:
        """No state of its own."""

    # -- credential detection --------------------------------------
    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> DetectedCredentials | None:
        """Read credentials from the local Codex CLI install.

        :param credential_home: Optional Codex state directory
            (``CODEX_HOME``). Defaults to ``$CODEX_HOME`` when set,
            otherwise ``~/.codex``.
        :return: Detected credentials, or ``None`` when no login
            is found on this machine.
        """
        blob = read_auth_blob(credential_home)
        if blob is None:
            return None
        return self._parse_blob(blob)

    @staticmethod
    def _parse_blob(
        blob: dict[str, Any],
    ) -> DetectedCredentials | None:
        """Pull credentials out of a Codex auth.json blob.

        :param blob: Parsed auth.json contents.
        :return: ``DetectedCredentials`` or ``None`` on missing keys.
        """
        tokens = blob.get("tokens") or {}
        access = tokens.get("access_token")
        if not access:
            return None
        token_claims = _decode_jwt_payload(access)
        auth_claims = _auth_claims(token_claims)
        account_id = tokens.get("account_id")
        if not isinstance(account_id, str):
            account_id = _claim_str(auth_claims, "chatgpt_account_id")
        plan = _claim_str(auth_claims, "chatgpt_plan_type") or "unknown"
        refresh = tokens.get("refresh_token")
        id_token = tokens.get("id_token")
        last_refresh = blob.get("last_refresh")
        return DetectedCredentials(
            access_token=access,
            provider_account_id=account_id,
            refresh_token=refresh if isinstance(refresh, str) else None,
            expires_at=_jwt_exp(token_claims),
            plan=plan,
            id_token=id_token if isinstance(id_token, str) else None,
            last_refresh=(
                last_refresh if isinstance(last_refresh, str) else None
            ),
        )

    # -- usage fetch -----------------------------------------------
    def fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Hit the Codex usage endpoint and parse the response.

        :param account: Account to query.
        :param http: Shared HTTP client.
        :return: Parsed :class:`UsageReport`.
        """
        account_id = account.provider_account_id
        if not account_id:
            account_id = _account_id_from_token(account.access_token)
            account.provider_account_id = account_id
        if not account_id:
            raise UsageError(
                "Missing Codex account id. Log in to Codex again, then "
                f"sidekick-usages refresh {account.label}."
            )
        data = http.get_json(
            USAGE_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {account.access_token}",
                "ChatGPT-Account-Id": account_id,
                "OpenAI-Beta": "codex",
                "User-Agent": USER_AGENT,
            },
        )
        rate_limit = data.get("rate_limit")
        if not isinstance(rate_limit, dict):
            rate_limit = data
        windows = _rate_limit_windows(rate_limit)
        windows.extend(_additional_rate_limit_windows(data))
        return UsageReport(
            windows=windows,
            plan=_response_plan(data),
            raw=data,
        )

    # -- refresh ---------------------------------------------------
    def refresh_token(
        self,
        account: Account,
        http: HttpClient,
    ) -> bool:
        """Exchange a refresh token for a new access token.

        :param account: Account whose access_token to refresh.
            Mutated in-place on success.
        :param http: Shared HTTP client.
        :return: True on success, False if no refresh token is
            available or the exchange failed.
        """
        if not account.refresh_token:
            return False
        try:
            response = http.post_form(
                OAUTH_REFRESH_ENDPOINT,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": account.refresh_token,
                    "client_id": OAUTH_CLIENT_ID,
                },
            )
        except AuthError:
            return False
        if not _apply_refresh_response(account, response):
            return False
        account.codex_last_refresh = _now_utc_z()
        if account.codex_home:
            write_account_auth_file(account, Path(account.codex_home))
        return True

    # -- setup-token -----------------------------------------------
    def run_setup_token(self) -> str | None:
        """Codex has no long-lived token generator.

        :raises UnsupportedOperationError: Always.
        """
        raise UnsupportedOperationError(
            "Codex CLI doesn't expose a long-lived token generator. "
            "Run `codex login` then `sidekick-usages add codex`."
        )


def default_codex_home() -> Path:
    """Return the Codex home used by default credential detection."""
    configured = os.environ.get(CODEX_HOME_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def codex_auth_path(credential_home: Path | None = None) -> Path:
    """Return the auth.json path for a Codex home or auth file path.

    :param credential_home: Codex state directory. Passing an
        ``auth.json`` path is tolerated for tests and scripting.
    :return: Path to the auth file.
    """
    home = default_codex_home() if credential_home is None else credential_home
    home = home.expanduser()
    if home.name == CODEX_AUTH_FILE:
        return home
    return home / CODEX_AUTH_FILE


def read_auth_blob(
    credential_home: Path | None = None,
) -> dict[str, Any] | None:
    """Read a Codex auth.json blob.

    :param credential_home: Optional Codex state directory.
    :return: Parsed auth blob or ``None`` when absent/malformed.
    """
    path = codex_auth_path(credential_home)
    if not path.exists():
        return None
    try:
        blob = json.loads(path.read_text())
    except json.JSONDecodeError, OSError:
        return None
    return blob if isinstance(blob, dict) else None


def ensure_file_auth_home(codex_home: Path) -> None:
    """Create a Codex home configured for file-backed auth.

    :param codex_home: Target Codex state directory.
    """
    codex_home = codex_home.expanduser()
    codex_home.mkdir(parents=True, exist_ok=True)
    config_path = codex_home / CODEX_CONFIG_FILE
    line = CODEX_FILE_AUTH_CONFIG
    if not config_path.exists():
        config_path.write_text(f"{line}\n")
    else:
        text = config_path.read_text()
        config_re = re.compile(
            r'(?m)^\s*cli_auth_credentials_store\s*=\s*"[^"]*"\s*$'
        )
        if config_re.search(text):
            updated = config_re.sub(line, text, count=1)
        else:
            updated = text.rstrip() + f"\n{line}\n"
        if updated != text:
            config_path.write_text(updated)
    if platform.system() != "Windows":
        os.chmod(codex_home, stat.S_IRWXU)
        os.chmod(config_path, stat.S_IRUSR | stat.S_IWUSR)


def write_account_auth_file(
    account: Account,
    codex_home: Path,
    *,
    source_blob: dict[str, Any] | None = None,
) -> bool:
    """Write a CLI-compatible Codex auth.json for one saved account.

    The Codex CLI requires ``id_token`` in addition to the access and
    refresh tokens. If neither the account nor a source auth blob has
    that field, the function refuses to write a misleading partial
    file.

    :param account: Saved Codex account.
    :param codex_home: Target Codex home.
    :param source_blob: Optional existing auth blob to preserve fields
        from, such as ``auth_mode``.
    :return: True when a complete auth file was written.
    """
    if account.provider_id != "codex" or not account.refresh_token:
        return False
    existing = source_blob or read_auth_blob(codex_home) or {}
    existing_tokens = _auth_tokens(existing)
    id_token = _auth_id_token(account, existing_tokens)
    account_id = _auth_account_id(account)
    if not id_token or not account_id:
        return False

    blob = dict(existing)
    blob["auth_mode"] = _auth_mode(existing)
    blob["last_refresh"] = _auth_last_refresh(account, existing)
    blob["tokens"] = _updated_auth_tokens(
        existing_tokens,
        account,
        id_token,
        account_id,
    )

    ensure_file_auth_home(codex_home)
    path = codex_auth_path(codex_home)
    path.write_text(json.dumps(blob, indent=2))
    if platform.system() != "Windows":
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    account.codex_home = str(codex_home.expanduser())
    account.codex_id_token = id_token
    account.codex_last_refresh = str(blob["last_refresh"])
    account.provider_account_id = account_id
    return True


def auth_blob_matches_account(
    blob: dict[str, Any],
    account: Account,
) -> bool:
    """Return whether a Codex auth blob belongs to ``account``."""
    tokens = blob.get("tokens")
    if not isinstance(tokens, dict):
        return False
    account_id = tokens.get("account_id")
    if not isinstance(account_id, str):
        access = tokens.get("access_token")
        if isinstance(access, str):
            account_id = _account_id_from_token(access)
    return bool(
        account.provider_account_id
        and account_id == account.provider_account_id
    )


def _auth_tokens(existing: dict[str, Any]) -> dict[str, Any]:
    """Return a mutable copy of auth.json tokens."""
    tokens = existing.get("tokens")
    return dict(tokens) if isinstance(tokens, dict) else {}


def _auth_id_token(
    account: Account,
    existing_tokens: dict[str, Any],
) -> str | None:
    """Resolve the id token needed for Codex CLI auth.json."""
    if account.codex_id_token:
        return account.codex_id_token
    existing_id = existing_tokens.get("id_token")
    return existing_id if isinstance(existing_id, str) else None


def _auth_account_id(account: Account) -> str | None:
    """Resolve the Codex account id for auth.json."""
    if account.provider_account_id:
        return account.provider_account_id
    return _account_id_from_token(account.access_token)


def _auth_mode(existing: dict[str, Any]) -> str:
    """Resolve auth mode for auth.json."""
    auth_mode = existing.get("auth_mode")
    return auth_mode if isinstance(auth_mode, str) else "chatgpt"


def _auth_last_refresh(
    account: Account,
    existing: dict[str, Any],
) -> str:
    """Resolve the last_refresh value for auth.json."""
    if account.codex_last_refresh:
        return account.codex_last_refresh
    existing_last_refresh = existing.get("last_refresh")
    if isinstance(existing_last_refresh, str):
        return existing_last_refresh
    return _now_utc_z()


def _updated_auth_tokens(
    existing_tokens: dict[str, Any],
    account: Account,
    id_token: str,
    account_id: str,
) -> dict[str, Any]:
    """Build the auth.json tokens object for a saved account."""
    tokens = dict(existing_tokens)
    tokens.update(
        {
            "access_token": account.access_token,
            "refresh_token": account.refresh_token,
            "id_token": id_token,
            "account_id": account_id,
        }
    )
    return tokens


def _apply_refresh_response(
    account: Account,
    response: dict[str, Any],
) -> bool:
    """Apply a successful OAuth refresh response to an account."""
    new_token = response.get("access_token")
    if not isinstance(new_token, str):
        return False
    account.access_token = new_token
    account_id = _account_id_from_token(new_token)
    if account_id:
        account.provider_account_id = account_id
    plan = _plan_from_token(new_token)
    if plan:
        account.plan = plan
    new_refresh = response.get("refresh_token")
    if isinstance(new_refresh, str):
        account.refresh_token = new_refresh
    new_id_token = response.get("id_token")
    if isinstance(new_id_token, str):
        account.codex_id_token = new_id_token
    _apply_refresh_expiry(account, response, new_token)
    return True


def _apply_refresh_expiry(
    account: Account,
    response: dict[str, Any],
    new_token: str,
) -> None:
    """Apply refresh expiry metadata to an account."""
    expires_in = response.get("expires_in")
    if isinstance(expires_in, int):
        account.expires_at = int(time.time()) + expires_in
        return
    exp = _jwt_exp(_decode_jwt_payload(new_token))
    if exp is not None:
        account.expires_at = exp


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload without validating the signature."""
    parts = token.split(".")
    if len(parts) < JWT_MIN_PARTS:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        raw = urlsafe_b64decode(payload)
        decoded = json.loads(raw.decode("utf-8"))
    except B64Error, UnicodeDecodeError, json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def _now_utc_z() -> str:
    """Return a Codex auth.json-style UTC timestamp."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _auth_claims(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Return the nested OpenAI auth claim from a JWT payload."""
    if not payload:
        return {}
    claims = payload.get("https://api.openai.com/auth")
    return claims if isinstance(claims, dict) else {}


def _claim_str(claims: dict[str, Any], key: str) -> str | None:
    """Read one string claim defensively."""
    value = claims.get(key)
    return value if isinstance(value, str) and value else None


def _jwt_exp(payload: dict[str, Any] | None) -> int | None:
    """Extract a Unix-seconds expiry from decoded JWT claims."""
    if not payload:
        return None
    exp = payload.get("exp")
    return exp if isinstance(exp, int) else None


def _account_id_from_token(token: str) -> str | None:
    """Extract the ChatGPT account id from an access token."""
    return _claim_str(
        _auth_claims(_decode_jwt_payload(token)),
        "chatgpt_account_id",
    )


def _plan_from_token(token: str) -> str | None:
    """Extract the ChatGPT plan tag from an access token."""
    return _claim_str(
        _auth_claims(_decode_jwt_payload(token)),
        "chatgpt_plan_type",
    )


def _usage_window(name: str, window: object) -> UsageWindow | None:
    """Convert one Codex rate-limit window into a UsageWindow."""
    if not isinstance(window, dict):
        return None
    window_data = cast("dict[str, Any]", window)
    reset = window_data.get("resets_at")
    if not isinstance(reset, str):
        reset = _epoch_to_iso(window_data.get("reset_at"))
    return UsageWindow(
        name=name,
        utilization=float(window_data.get("used_percent") or 0),
        resets_at=reset,
    )


def _rate_limit_windows(rate_limit: dict[str, Any]) -> list[UsageWindow]:
    """Parse standard Codex 5h and 7d windows."""
    windows: list[UsageWindow] = []
    primary = _usage_window("5h", rate_limit.get("primary_window"))
    if primary:
        windows.append(primary)
    secondary = _usage_window("7d", rate_limit.get("secondary_window"))
    if secondary:
        windows.append(secondary)
    return windows


def _additional_rate_limit_windows(data: dict[str, Any]) -> list[UsageWindow]:
    """Parse provider-specific extra Codex rate-limit windows."""
    windows: list[UsageWindow] = []
    for extra in data.get("additional_rate_limits") or []:
        if not isinstance(extra, dict):
            continue
        extra_data = cast("dict[str, Any]", extra)
        label = extra_data.get("limit_name") or extra_data.get("label")
        label = label or extra_data.get("model") or "?"
        extra_rate_limit = extra_data.get("rate_limit")
        if isinstance(extra_rate_limit, dict):
            windows.extend(
                _rate_limit_windows_with_prefix(
                    str(label),
                    cast("dict[str, Any]", extra_rate_limit),
                )
            )
            continue
        legacy_extra = _usage_window(str(label), extra_data)
        if legacy_extra:
            windows.append(legacy_extra)
    return windows


def _rate_limit_windows_with_prefix(
    label: str,
    rate_limit: dict[str, Any],
) -> list[UsageWindow]:
    """Parse extra 5h and 7d windows under a named limit."""
    windows: list[UsageWindow] = []
    primary = _usage_window(f"{label} 5h", rate_limit.get("primary_window"))
    if primary:
        windows.append(primary)
    secondary = _usage_window(
        f"{label} 7d", rate_limit.get("secondary_window")
    )
    if secondary:
        windows.append(secondary)
    return windows


def _epoch_to_iso(value: object) -> str | None:
    """Convert epoch seconds into an ISO timestamp for rendering."""
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat()


def _response_plan(data: dict[str, Any]) -> str | None:
    """Extract plan from old or current Codex usage response shapes."""
    plan = data.get("plan_type") or data.get("plan")
    return plan if isinstance(plan, str) else None

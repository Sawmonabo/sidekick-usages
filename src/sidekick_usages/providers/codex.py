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
from base64 import urlsafe_b64decode
from binascii import Error as B64Error
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import (
    Expiry,
    InvalidExpiry,
    KnownExpiry,
    UnknownExpiry,
)
from sidekick_usages.core.models import (
    Account,
    CodexCredentials,
    DetectedCredentials,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.errors import (
    AuthError,
    InvalidPayloadError,
    UnsupportedOperationError,
    UsageError,
)
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.providers.base import Provider
from sidekick_usages.serialization import (
    JsonObject,
    JsonValue,
    decode_json_object,
)

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

    id = ProviderId.CODEX
    display_name = "Codex CLI"
    token_pattern = TOKEN_RE

    def __init__(self, clock: Clock) -> None:
        """Use an injected wall clock for refreshed credentials."""
        self.clock = clock

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
        blob: JsonObject,
    ) -> DetectedCredentials | None:
        """Pull credentials out of a Codex auth.json blob.

        :param blob: Parsed auth.json contents.
        :return: ``DetectedCredentials`` or ``None`` on missing keys.
        """
        tokens_value = blob.get("tokens")
        tokens = tokens_value if isinstance(tokens_value, dict) else {}
        access = tokens.get("access_token")
        if not isinstance(access, str) or not access:
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
            credentials=CodexCredentials(
                access_token=access,
                account_id=account_id,
                refresh_token=refresh if isinstance(refresh, str) else None,
                expiry=_jwt_expiry(token_claims),
                id_token=id_token if isinstance(id_token, str) else None,
                auth_last_refresh=(
                    last_refresh if isinstance(last_refresh, str) else None
                ),
            ),
            plan=plan,
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
        credentials = _codex_credentials(account)
        account_id = credentials.account_id
        if not account_id:
            account_id = _account_id_from_token(account.access_token)
            account.credentials = replace(credentials, account_id=account_id)
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
            windows=tuple(windows),
            plan=_response_plan(data),
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
        credentials = _codex_credentials(account)
        if not credentials.refresh_token:
            return False
        try:
            response = http.post_form(
                OAUTH_REFRESH_ENDPOINT,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": credentials.refresh_token,
                    "client_id": OAUTH_CLIENT_ID,
                },
                operation=HttpOperation.CODEX_REFRESH,
            )
        except AuthError:
            return False
        new_token = response.get("access_token")
        if not isinstance(new_token, str) or not new_token:
            return False
        reference_time = self.clock.now()
        updated, plan = _updated_refresh_credentials(
            credentials,
            response,
            new_token,
            reference_time,
        )
        account.credentials = replace(
            updated,
            auth_last_refresh=_utc_z(reference_time),
        )
        if plan:
            account.plan = plan
        if account.codex_home:
            write_account_auth_file(
                account,
                Path(account.codex_home),
                reference_time=reference_time,
            )
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
) -> JsonObject | None:
    """Read a Codex auth.json blob.

    :param credential_home: Optional Codex state directory.
    :return: Parsed auth blob or ``None`` when absent/malformed.
    """
    path = codex_auth_path(credential_home)
    if not path.exists():
        return None
    try:
        return decode_json_object(path.read_bytes())
    except InvalidPayloadError, OSError:
        return None


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
    reference_time: datetime,
    source_blob: JsonObject | None = None,
) -> bool:
    """Write a CLI-compatible Codex auth.json for one saved account.

    The Codex CLI requires ``id_token`` in addition to the access and
    refresh tokens. If neither the account nor a source auth blob has
    that field, the function refuses to write a misleading partial
    file.

    :param account: Saved Codex account.
    :param codex_home: Target Codex home.
    :param reference_time: Aware UTC time for a missing refresh timestamp.
    :param source_blob: Optional existing auth blob to preserve fields
        from, such as ``auth_mode``.
    :return: True when a complete auth file was written.
    """
    if (
        account.provider_id is not ProviderId.CODEX
        or not account.refresh_token
    ):
        return False
    credentials = _codex_credentials(account)
    existing = source_blob or read_auth_blob(codex_home) or {}
    existing_tokens = _auth_tokens(existing)
    id_token = _auth_id_token(account, existing_tokens)
    account_id = _auth_account_id(account)
    if not id_token or not account_id:
        return False

    blob = dict(existing)
    blob["auth_mode"] = _auth_mode(existing)
    blob["last_refresh"] = _auth_last_refresh(
        account,
        existing,
        reference_time,
    )
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
    account.credentials = replace(
        credentials,
        auth_home=str(codex_home.expanduser()),
        id_token=id_token,
        auth_last_refresh=str(blob["last_refresh"]),
        account_id=account_id,
    )
    return True


def auth_blob_matches_account(
    blob: JsonObject,
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


def _auth_tokens(existing: JsonObject) -> JsonObject:
    """Return a mutable copy of auth.json tokens."""
    tokens = existing.get("tokens")
    return dict(tokens) if isinstance(tokens, dict) else {}


def _auth_id_token(
    account: Account, existing_tokens: JsonObject
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


def _auth_mode(existing: JsonObject) -> str:
    """Resolve auth mode for auth.json."""
    auth_mode = existing.get("auth_mode")
    return auth_mode if isinstance(auth_mode, str) else "chatgpt"


def _auth_last_refresh(
    account: Account,
    existing: JsonObject,
    reference_time: datetime,
) -> str:
    """Resolve the last_refresh value for auth.json."""
    if account.codex_last_refresh:
        return account.codex_last_refresh
    existing_last_refresh = existing.get("last_refresh")
    if isinstance(existing_last_refresh, str):
        return existing_last_refresh
    return _utc_z(reference_time)


def _updated_auth_tokens(
    existing_tokens: JsonObject,
    account: Account,
    id_token: str,
    account_id: str,
) -> JsonObject:
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


def _updated_refresh_credentials(
    credentials: CodexCredentials,
    response: JsonObject,
    new_token: str,
    reference_time: datetime,
) -> tuple[CodexCredentials, str | None]:
    """Build one validated atomic Codex credential refresh."""
    account_id = _account_id_from_token(new_token) or credentials.account_id
    plan = _plan_from_token(new_token)
    new_refresh = response.get("refresh_token")
    new_id_token = response.get("id_token")
    if "refresh_token" in response and (
        not isinstance(new_refresh, str) or not new_refresh
    ):
        raise InvalidPayloadError
    if "id_token" in response and (
        not isinstance(new_id_token, str) or not new_id_token
    ):
        raise InvalidPayloadError
    return (
        replace(
            credentials,
            access_token=new_token,
            account_id=account_id,
            refresh_token=(
                new_refresh
                if isinstance(new_refresh, str)
                else credentials.refresh_token
            ),
            id_token=(
                new_id_token
                if isinstance(new_id_token, str)
                else credentials.id_token
            ),
            expiry=_refresh_expiry(response, new_token, reference_time),
        ),
        plan,
    )


def _refresh_expiry(
    response: JsonObject,
    new_token: str,
    reference_time: datetime,
) -> Expiry:
    """Normalize refreshed Codex expiry without floating-point epochs."""
    expires_in = response.get("expires_in")
    if expires_in is not None:
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or expires_in < 0
        ):
            raise InvalidPayloadError
        if reference_time.tzinfo is None or reference_time.utcoffset() is None:
            raise ValueError("Codex refresh time must be timezone-aware.")
        base = reference_time.astimezone(UTC).replace(microsecond=0)
        return KnownExpiry(base + timedelta(seconds=expires_in))
    expiry = _jwt_expiry(_decode_jwt_payload(new_token))
    if isinstance(expiry, InvalidExpiry):
        raise InvalidPayloadError
    return expiry


def _decode_jwt_payload(token: str) -> JsonObject | None:
    """Decode a JWT payload without validating the signature."""
    parts = token.split(".")
    if len(parts) < JWT_MIN_PARTS:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        raw = urlsafe_b64decode(payload)
        return decode_json_object(raw)
    except B64Error, InvalidPayloadError:
        return None


def _utc_z(value: datetime) -> str:
    """Return a Codex auth.json-style UTC timestamp."""
    return value.isoformat().replace("+00:00", "Z")


def _auth_claims(payload: JsonObject | None) -> JsonObject:
    """Return the nested OpenAI auth claim from a JWT payload."""
    if not payload:
        return {}
    claims = payload.get("https://api.openai.com/auth")
    return claims if isinstance(claims, dict) else {}


def _claim_str(claims: JsonObject, key: str) -> str | None:
    """Read one string claim defensively."""
    value = claims.get(key)
    return value if isinstance(value, str) and value else None


def _jwt_expiry(payload: JsonObject | None) -> Expiry:
    """Normalize a strict Unix-seconds expiry from decoded JWT claims."""
    if payload is None:
        return InvalidExpiry()
    exp = payload.get("exp")
    if exp is None:
        return UnknownExpiry()
    if isinstance(exp, bool) or not isinstance(exp, int) or exp < 0:
        return InvalidExpiry()
    try:
        return KnownExpiry(
            datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=exp)
        )
    except OverflowError:
        return InvalidExpiry()


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


def _usage_window(name: str, window: JsonValue | None) -> UsageWindow | None:
    """Convert one Codex rate-limit window into a UsageWindow."""
    if not isinstance(window, dict):
        return None
    reset = _provider_time(window.get("resets_at"))
    if reset is None:
        reset = _epoch_to_time(window.get("reset_at"))
    return UsageWindow(
        name=name,
        utilization=_utilization(window.get("used_percent")),
        resets_at=reset,
    )


def _rate_limit_windows(rate_limit: JsonObject) -> list[UsageWindow]:
    """Parse standard Codex 5h and 7d windows."""
    windows: list[UsageWindow] = []
    primary = _usage_window("5h", rate_limit.get("primary_window"))
    if primary:
        windows.append(primary)
    secondary = _usage_window("7d", rate_limit.get("secondary_window"))
    if secondary:
        windows.append(secondary)
    return windows


def _additional_rate_limit_windows(data: JsonObject) -> list[UsageWindow]:
    """Parse provider-specific extra Codex rate-limit windows."""
    windows: list[UsageWindow] = []
    extras = data.get("additional_rate_limits")
    if not isinstance(extras, list):
        return windows
    for extra in extras:
        if not isinstance(extra, dict):
            continue
        label = extra.get("limit_name") or extra.get("label")
        label = label or extra.get("model") or "?"
        extra_rate_limit = extra.get("rate_limit")
        if isinstance(extra_rate_limit, dict):
            windows.extend(
                _rate_limit_windows_with_prefix(
                    str(label),
                    extra_rate_limit,
                )
            )
            continue
        legacy_extra = _usage_window(str(label), extra)
        if legacy_extra:
            windows.append(legacy_extra)
    return windows


def _rate_limit_windows_with_prefix(
    label: str,
    rate_limit: JsonObject,
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


def _epoch_to_time(value: JsonValue | None) -> datetime | None:
    """Convert provider epoch seconds into an aware UTC timestamp."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except OverflowError, OSError, ValueError:
        return None


def _response_plan(data: JsonObject) -> str | None:
    """Extract plan from old or current Codex usage response shapes."""
    plan = data.get("plan_type") or data.get("plan")
    return plan if isinstance(plan, str) else None


def _provider_time(value: JsonValue | None) -> datetime | None:
    """Normalize one optional Codex response timestamp."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _utilization(value: JsonValue | None) -> float:
    """Return one numeric utilization percentage."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _codex_credentials(account: Account) -> CodexCredentials:
    """Return Codex credentials or reject an incompatible account."""
    credentials = account.credentials
    if isinstance(credentials, CodexCredentials):
        return credentials
    raise UsageError(f"Account {account.label!r} is not a Codex account.")

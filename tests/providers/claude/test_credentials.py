"""Claude credential-mode invariants and provider routing."""

from collections.abc import Mapping
from datetime import timedelta

import pytest

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    DetectedCredentials,
)
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.http.client import HttpClient
from sidekick_usages.http.models import HttpHeaderResponse
from sidekick_usages.http.types import HttpOperation
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureCause,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.provider import ClaudeProvider
from sidekick_usages.providers.claude.schema.credentials import (
    parse_credentials_blob,
)
from sidekick_usages.providers.claude.usage import USAGE_URL
from sidekick_usages.serialization.json import JsonObject
from tests.support.accounts import authenticated_account
from tests.support.time import REFERENCE_TIME, FixedClock

_ACCESS_EXPIRY = REFERENCE_TIME + timedelta(hours=1)
_REFRESH_EXPIRY = REFERENCE_TIME + timedelta(days=30)
_ACCESS_EXPIRY_MS = int(_ACCESS_EXPIRY.timestamp() * 1000)
_REFRESH_EXPIRY_MS = int(_REFRESH_EXPIRY.timestamp() * 1000)


class _RouteHttp(HttpClient):
    """Record which Claude usage boundary was selected."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str]] = []

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
        del headers
        self.calls.append(("GET", url))
        return {}

    def post_capture_headers(
        self,
        url: str,
        json_body: JsonObject,
        headers: Mapping[str, str],
        *,
        operation: HttpOperation,
    ) -> HttpHeaderResponse:
        del json_body, headers
        assert operation is HttpOperation.CLAUDE_PROBE
        self.calls.append(("POST", url))
        return HttpHeaderResponse(200, {})


def _setup_credentials() -> ClaudeSetupTokenCredentials:
    return ClaudeSetupTokenCredentials(access_token="sk-ant-oat01-setup")


def _login_credentials(
    *,
    with_identity: bool = False,
) -> ClaudeLoginCredentials:
    return ClaudeLoginCredentials(
        access_token="sk-ant-oat01-login",
        refresh_token="refresh-login",
        access_expiry=KnownExpiry(_ACCESS_EXPIRY),
        refresh_expiry=KnownExpiry(_REFRESH_EXPIRY),
        scopes=("user:inference", "user:profile"),
        identity=(
            ClaudeLoginIdentity(
                account_id="account-123",
                organization_id="organization-456",
            )
            if with_identity
            else None
        ),
    )


def _account(credentials: ClaudeCredentials, label: str) -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=credentials,
    )


def test_closed_variants_make_setup_state_unrepresentable() -> None:
    """Setup credentials expose no login-only state or secret repr data."""
    credentials = _setup_credentials()

    assert not hasattr(credentials, "refresh_token")
    assert not hasattr(credentials, "access_expiry")
    assert not hasattr(credentials, "refresh_expiry")
    assert not hasattr(credentials, "scopes")
    assert not hasattr(credentials, "identity")
    assert "sk-ant-oat01-setup" not in repr(credentials)


def test_login_domain_rejects_duplicate_scopes() -> None:
    """A login cannot carry an ambiguous capability set."""
    with pytest.raises(ValueError, match="scope"):
        ClaudeLoginCredentials(
            access_token="sk-ant-oat01-login",
            refresh_token="refresh-login",
            access_expiry=KnownExpiry(_ACCESS_EXPIRY),
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile", "user:profile"),
        )


def test_native_login_parses_both_expiries_and_stable_identity() -> None:
    """Provider-owned login state becomes one complete login variant."""
    detected = parse_credentials_blob(
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-oat01-native",
                "refreshToken": "refresh-native",
                "expiresAt": _ACCESS_EXPIRY_MS,
                "refreshTokenExpiresAt": _REFRESH_EXPIRY_MS,
                "scopes": ["user:profile", "user:inference"],
                "tokenAccount": {
                    "accountUuid": "account-123",
                    "organizationUuid": "organization-456",
                },
            }
        }
    )
    credentials = detected.credentials

    assert isinstance(credentials, ClaudeLoginCredentials)
    assert credentials.access_expiry == KnownExpiry(_ACCESS_EXPIRY)
    assert credentials.refresh_expiry == KnownExpiry(_REFRESH_EXPIRY)
    identity = credentials.identity
    assert identity is not None
    assert identity.account_id == "account-123"
    assert identity.organization_id == "organization-456"
    assert "account-123" not in repr(credentials)
    assert "organization-456" not in repr(credentials)


@pytest.mark.parametrize(
    "oauth_update",
    [
        {"refreshToken": None},
        {"expiresAt": None},
        {"scopes": None},
        {"scopes": ["user:inference"]},
        {"scopes": ["user:profile", "user:profile"]},
        {"expiresAt": True},
        {"refreshTokenExpiresAt": -1},
        {"refreshTokenExpiresAt": None},
        {"tokenAccount": {"accountUuid": "account-only"}},
        {
            "tokenAccount": {
                "accountUuid": "account-123",
                "organizationUuid": "",
            }
        },
    ],
)
def test_native_login_rejects_incomplete_or_inconsistent_state(
    oauth_update: JsonObject,
) -> None:
    """Native credential files never degrade into setup credentials."""
    oauth: JsonObject = {
        "accessToken": "sk-ant-oat01-native",
        "refreshToken": "refresh-native",
        "expiresAt": _ACCESS_EXPIRY_MS,
        "scopes": ["user:profile", "user:inference"],
    }
    oauth.update(oauth_update)

    with pytest.raises(ProviderBoundaryError):
        parse_credentials_blob({"claudeAiOauth": oauth})


def test_credentials_from_token_constructs_only_setup_variant() -> None:
    """Explicit token input is the sole setup-token construction boundary."""
    detected = ClaudeProvider(FixedClock()).credentials_from_token(
        "sk-ant-oat01-manual"
    )

    assert isinstance(detected, DetectedCredentials)
    assert isinstance(detected.credentials, ClaudeSetupTokenCredentials)


def test_refresh_missing_token_is_explicit_and_does_not_mutate() -> None:
    """Setup credentials cannot enter subscription refresh."""
    account = _account(_setup_credentials(), "claude-team")
    original = account.credentials

    result = ClaudeProvider(FixedClock()).refresh_credentials(
        authenticated_account(account),
        HttpClient(),
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.MISSING
    assert result.cause is ProviderFailureCause.MISSING_REFRESH_CREDENTIAL
    assert account.credentials is original


def test_login_scope_order_and_missing_identity_keep_oauth_route() -> None:
    """Capability ordering and optional identity cannot change login kind."""
    account = _account(_login_credentials(), "native-login")
    http = _RouteHttp()

    ClaudeProvider(FixedClock()).validate_credentials(account, http)

    assert http.calls == [("GET", USAGE_URL)]

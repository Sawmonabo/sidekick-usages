"""Load-bearing Codex auth, refresh, usage, and isolation tests."""

import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    CodexCredentials,
    DetectedCredentials,
)
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.errors import AuthError
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.persistence.private_credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
    RefreshSuccess,
)
from sidekick_usages.providers.codex import schemas
from sidekick_usages.providers.codex.auth import (
    PreparedCodexAuthBundle,
    auth_blob_account_id,
    prepare_export_bundle,
    prepare_private_bundle,
)
from sidekick_usages.providers.codex.provider import CodexProvider
from sidekick_usages.providers.codex.schemas import validate_refresh_payload
from sidekick_usages.serialization import JsonObject
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    authenticated_account,
)

DETECTED_EXP = 1_800_000_000
REFRESH_EXP = 1_900_000_000
PRIMARY_RESET = 1_770_003_600
PRIMARY_USED = 12
EXTRA_SECONDARY_USED = 78


def _jwt(payload: dict[str, object]) -> str:
    """Build an unsigned JWT-shaped provider fixture."""
    header = {"alg": "none", "typ": "JWT"}

    def encode(value: Mapping[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode(header)}.{encode(payload)}.sig"


def _access_token(
    *,
    account_id: str = "acct_123",
    expiry: int = REFRESH_EXP,
    plan: str = "pro",
) -> str:
    return _jwt(
        {
            "exp": expiry,
            "https://api.openai.com/auth": {
                "chatgpt_account_id": account_id,
                "chatgpt_plan_type": plan,
            },
        }
    )


def _account(*, auth_home: str | None = None) -> Account:
    return Account(
        label=AccountLabel("codex-pro"),
        credentials=CodexCredentials(
            access_token="access-old",
            refresh_token="refresh-old",
            expiry=KnownExpiry(datetime.fromtimestamp(1_700_000_000, UTC)),
            account_id="acct_123",
            auth_home=auth_home,
        ),
        plan="unknown",
    )


class _UsageHttp(HttpClient):
    """Record GET headers and return one usage payload."""

    def __init__(self, payload: JsonObject) -> None:
        self.payload = payload
        self.headers: dict[str, str] | None = None

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
        del url
        self.headers = dict(headers)
        return self.payload


class _RefreshHttp(HttpClient):
    """Record refresh form data and return or raise one result."""

    def __init__(
        self,
        payload: JsonObject | None = None,
        error: AuthError | None = None,
    ) -> None:
        self.payload = payload or {}
        self.error = error
        self.data: dict[str, str] | None = None

    def post_form(
        self,
        url: str,
        data: Mapping[str, str],
        headers: Mapping[str, str] | None = None,
        *,
        operation: HttpOperation,
    ) -> JsonObject:
        del url, headers
        assert operation is HttpOperation.CODEX_REFRESH
        self.data = dict(data)
        if self.error is not None:
            raise self.error
        return self.payload


def _usage_payload() -> JsonObject:
    return {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {
                "used_percent": PRIMARY_USED,
                "reset_at": PRIMARY_RESET,
            },
            "secondary_window": {
                "used_percent": 34,
                "reset_at": 1_770_604_800,
            },
        },
        "additional_rate_limits": [
            {
                "limit_name": "gpt-5.1-codex",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 56,
                        "reset_at": 1_770_007_200,
                    },
                    "secondary_window": {
                        "used_percent": EXTRA_SECONDARY_USED,
                        "reset_at": 1_770_691_200,
                    },
                },
            }
        ],
    }


def test_detection_validates_auth_identity_expiry_and_metadata(
    tmp_path: Path,
) -> None:
    """One valid auth source yields normalized provider-owned state."""
    codex_home = tmp_path / "codex-a"
    codex_home.mkdir()
    access = _access_token(expiry=DETECTED_EXP)
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "last_refresh": "2026-06-12T00:00:00Z",
                "tokens": {
                    "access_token": access,
                    "refresh_token": "refresh-home",
                    "id_token": "id-token-home",
                },
            }
        )
    )

    result = CodexProvider(FixedClock()).detect_credentials(codex_home)

    assert isinstance(result, DetectedCredentials)
    assert result.access_token == access
    assert result.refresh_token == "refresh-home"
    assert result.provider_account_id == "acct_123"
    assert result.id_token == "id-token-home"
    assert result.last_refresh == "2026-06-12T00:00:00Z"
    assert result.expiry == KnownExpiry(
        datetime.fromtimestamp(DETECTED_EXP, UTC)
    )
    assert result.plan == "pro"


@pytest.mark.parametrize(
    ("content", "expected_kind"),
    [
        (None, ProviderFailureKind.MISSING),
        (b"{", ProviderFailureKind.MALFORMED),
        (
            b'{"tokens":{"refresh_token":"refresh-only"}}',
            ProviderFailureKind.INCOMPLETE,
        ),
        (
            json.dumps(
                {"tokens": {"access_token": _access_token(expiry=1)}}
            ).encode(),
            ProviderFailureKind.EXPIRED,
        ),
        (
            json.dumps(
                {
                    "tokens": {
                        "access_token": _access_token(),
                        "refresh_token": None,
                    }
                }
            ).encode(),
            ProviderFailureKind.MALFORMED,
        ),
        (
            json.dumps(
                {
                    "tokens": {
                        "access_token": _access_token(),
                        "id_token": None,
                    }
                }
            ).encode(),
            ProviderFailureKind.MALFORMED,
        ),
        (
            json.dumps(
                {
                    "tokens": {
                        "access_token": _access_token(),
                        "account_id": "acct_other",
                    }
                }
            ).encode(),
            ProviderFailureKind.IDENTITY_MISMATCH,
        ),
    ],
)
def test_detection_distinguishes_safe_source_failures(
    tmp_path: Path,
    content: bytes | None,
    expected_kind: ProviderFailureKind,
) -> None:
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    if content is not None:
        (codex_home / "auth.json").write_bytes(content)

    result = CodexProvider(FixedClock()).detect_credentials(codex_home)

    assert isinstance(result, ProviderFailure)
    assert result.kind is expected_kind
    assert "refresh-only" not in repr(result)
    assert "eyJ" not in repr(result)


@pytest.mark.parametrize(
    ("declared_id", "claim_id", "expected"),
    [
        ("acct_123", "acct_123", "acct_123"),
        ("acct_123", None, "acct_123"),
        (None, "acct_123", "acct_123"),
        (
            "acct_declared",
            "acct_claimed",
            ProviderFailureKind.IDENTITY_MISMATCH,
        ),
    ],
)
def test_auth_identity_resolution_never_prefers_a_conflict(
    declared_id: str | None,
    claim_id: str | None,
    expected: str | ProviderFailureKind,
) -> None:
    tokens: JsonObject = {}
    if declared_id is not None:
        tokens["account_id"] = declared_id
    if claim_id is not None:
        tokens["access_token"] = _access_token(account_id=claim_id)
    blob: JsonObject = {"tokens": tokens}

    if isinstance(expected, ProviderFailureKind):
        with pytest.raises(ProviderBoundaryError) as exc_info:
            auth_blob_account_id(blob)
        assert exc_info.value.failure.kind is expected
        return
    assert auth_blob_account_id(blob) == expected


@pytest.mark.parametrize(
    ("plan", "last_refresh", "expected_kind"),
    [
        ("é" * 128, "timestamp", None),
        ("é" * 129, "timestamp", ProviderFailureKind.MALFORMED),
        ("pro", "é" * 2048, None),
        ("pro", "é" * 2049, ProviderFailureKind.MALFORMED),
    ],
)
def test_auth_semantic_strings_use_utf8_byte_limits(
    tmp_path: Path,
    plan: str,
    last_refresh: str,
    expected_kind: ProviderFailureKind | None,
) -> None:
    codex_home = tmp_path / "codex-bounds"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "last_refresh": last_refresh,
                "tokens": {"access_token": _access_token(plan=plan)},
            }
        )
    )

    result = CodexProvider(FixedClock()).detect_credentials(codex_home)

    if expected_kind is None:
        assert isinstance(result, DetectedCredentials)
        assert result.plan == plan
        return
    assert isinstance(result, ProviderFailure)
    assert result.kind is expected_kind


def test_detection_reports_unreadable_auth(tmp_path: Path) -> None:
    auth_path = tmp_path / "auth.json"
    auth_path.mkdir()

    result = CodexProvider(FixedClock()).detect_credentials(auth_path)

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.UNREADABLE
    assert str(auth_path) not in repr(result)


def test_usage_validates_current_shape_and_required_headers() -> None:
    """Current usage becomes normalized windows with account-bound headers."""
    http = _UsageHttp(_usage_payload())

    report = CodexProvider(FixedClock()).validate_credentials(_account(), http)

    assert http.headers is not None
    assert http.headers["ChatGPT-Account-Id"] == "acct_123"
    assert http.headers["OpenAI-Beta"] == "codex"
    by_name = {window.name: window for window in report.windows}
    assert report.plan == "pro"
    assert by_name["5h"].utilization == PRIMARY_USED
    assert by_name["gpt-5.1-codex 7d"].utilization == EXTRA_SECONDARY_USED
    assert by_name["5h"].resets_at == datetime.fromtimestamp(
        PRIMARY_RESET,
        tz=UTC,
    )


def test_usage_rejects_malformed_window_without_exposing_input() -> None:
    raw_secret = "raw-token-body-should-never-escape"
    payload = _usage_payload()
    rate_limit = payload["rate_limit"]
    assert isinstance(rate_limit, dict)
    primary = rate_limit["primary_window"]
    assert isinstance(primary, dict)
    primary["used_percent"] = raw_secret

    with pytest.raises(ProviderBoundaryError) as caught:
        CodexProvider(FixedClock()).validate_credentials(
            _account(), _UsageHttp(payload)
        )

    assert caught.value.failure.kind is ProviderFailureKind.MALFORMED
    assert caught.value.failure.fields == (
        "rate_limit.primary_window.used_percent",
    )
    assert raw_secret not in repr(caught.value)
    assert raw_secret not in repr(caught.value.failure)


def test_refresh_returns_complete_replacement_without_hidden_mutation() -> (
    None
):
    """A refresh carries replacement state and leaves its input untouched."""
    account = _account()
    original = account.credentials
    http = _RefreshHttp(
        {
            "access_token": _access_token(),
            "refresh_token": "refresh-new",
            "id_token": "id-new",
            "expires_in": 60,
        }
    )

    result = CodexProvider(FixedClock()).refresh_credentials(
        authenticated_account(account),
        http,
    )

    assert isinstance(result, RefreshSuccess)
    assert isinstance(result.credentials, CodexCredentials)
    assert account.credentials is original
    assert http.data == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-old",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
    }
    assert result.credentials.access_token == _access_token()
    assert result.credentials.refresh_token == "refresh-new"
    assert result.credentials.expiry == KnownExpiry(
        REFERENCE_TIME.replace(microsecond=0) + timedelta(seconds=60)
    )
    assert result.plan == "pro"
    assert "access_token" not in repr(result)


@pytest.mark.parametrize("field_name", ["refresh_token", "id_token"])
@pytest.mark.parametrize(
    "invalid_value",
    [
        None,
        "",
        42,
        pytest.param("é" * 131_073, id="oversized-utf8"),
    ],
)
def test_refresh_rejects_invalid_present_optional_tokens(
    field_name: str,
    invalid_value: JsonObject | str | int | None,
) -> None:
    account = _account()
    payload: JsonObject = {"access_token": _access_token()}
    payload[field_name] = invalid_value

    result = CodexProvider(FixedClock()).refresh_credentials(
        authenticated_account(account),
        _RefreshHttp(payload),
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.MALFORMED


def test_refresh_payload_representation_hides_every_token() -> None:
    access_token = _access_token(account_id="acct_repr")
    refresh_token = "refresh-repr-secret"
    id_token = "id-repr-secret"

    payload = validate_refresh_payload(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
        }
    )

    rendered = repr(payload)
    assert access_token not in rendered
    assert refresh_token not in rendered
    assert id_token not in rendered


def test_oversized_access_token_is_rejected_before_jwt_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_decode(*_args: object, **_kwargs: object) -> bytes:
        pytest.fail("oversized access tokens must not reach base64 decoding")

    monkeypatch.setattr(schemas, "b64decode", unexpected_decode)

    result = CodexProvider(FixedClock()).credentials_from_token("x" * 262_145)

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.MALFORMED


@pytest.mark.parametrize(
    ("http", "expected_kind"),
    [
        (
            _RefreshHttp({"refresh_token": "raw-secret"}),
            ProviderFailureKind.INCOMPLETE,
        ),
        (
            _RefreshHttp({"access_token": _jwt({"exp": "invalid"})}),
            ProviderFailureKind.MALFORMED,
        ),
        (
            _RefreshHttp(
                {"access_token": _access_token(), "expires_in": None}
            ),
            ProviderFailureKind.MALFORMED,
        ),
        (
            _RefreshHttp({"access_token": _jwt({"exp": REFRESH_EXP})}),
            ProviderFailureKind.INCOMPLETE,
        ),
        (
            _RefreshHttp(
                {"access_token": _access_token(account_id="acct_other")}
            ),
            ProviderFailureKind.IDENTITY_MISMATCH,
        ),
        (
            _RefreshHttp(error=AuthError("raw-rejection-body")),
            ProviderFailureKind.REJECTED,
        ),
    ],
)
def test_refresh_failures_are_typed_atomic_and_secret_safe(
    http: _RefreshHttp,
    expected_kind: ProviderFailureKind,
) -> None:
    account = _account()
    original = account.credentials

    result = CodexProvider(FixedClock()).refresh_credentials(
        authenticated_account(account),
        http,
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is expected_kind
    assert account.credentials is original
    assert "raw-secret" not in repr(result)
    assert "raw-rejection-body" not in repr(result)


def test_refresh_keeps_external_codex_home_read_only(tmp_path: Path) -> None:
    """Saved-account refresh never writes a provider-native login home."""
    codex_home = tmp_path / "external-codex"
    codex_home.mkdir()
    auth_path = codex_home / "auth.json"
    original = b'{"tokens":{"access_token":"active-login"}}'
    auth_path.write_bytes(original)
    account = _account(auth_home=str(codex_home))

    result = CodexProvider(FixedClock()).refresh_credentials(
        authenticated_account(account),
        _RefreshHttp({"access_token": _access_token()}),
    )

    assert isinstance(result, RefreshSuccess)
    assert auth_path.read_bytes() == original
    assert account.access_token == "access-old"


@pytest.mark.parametrize(
    "collision",
    [
        "active-directory",
        "active-auth-file",
        "active-symlink",
        "source-directory",
        "source-auth-file",
        "source-symlink",
    ],
)
def test_export_rejects_protected_auth_path_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    collision: str,
) -> None:
    active_home = tmp_path / "active-codex"
    active_home.mkdir()
    active_auth = active_home / "auth.json"
    active_auth.write_bytes(b"active-login-sentinel")
    source_home = tmp_path / "source-codex"
    source_home.mkdir()
    source_auth = source_home / "auth.json"
    source_auth.write_bytes(b"source-login-sentinel")
    active_alias = tmp_path / "active-alias"
    source_alias = tmp_path / "source-alias"
    if collision == "active-symlink":
        try:
            active_alias.symlink_to(active_home, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable")
    elif collision == "source-symlink":
        try:
            source_alias.symlink_to(source_home, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlinks are unavailable")
    configured = (
        active_auth if collision == "active-auth-file" else active_home
    )
    monkeypatch.setenv("CODEX_HOME", str(configured))
    target = {
        "active-directory": active_home,
        "active-auth-file": active_home,
        "active-symlink": active_alias,
        "source-directory": source_home,
        "source-auth-file": source_home,
        "source-symlink": source_alias,
    }[collision]
    if collision == "source-auth-file":
        sources = (source_auth,)
    elif collision.startswith("source-"):
        sources = (source_home,)
    else:
        sources = ()

    result = prepare_export_bundle(
        _account(),
        target,
        source_homes=sources,
        existing_config=None,
        reference_time=REFERENCE_TIME,
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.UNSUPPORTED
    assert active_auth.read_bytes() == b"active-login-sentinel"
    assert source_auth.read_bytes() == b"source-login-sentinel"


def test_export_requires_a_home_directory_target(
    tmp_path: Path,
) -> None:
    result = prepare_export_bundle(
        _account(),
        tmp_path / "isolated" / "auth.json",
        source_homes=(),
        existing_config=None,
        reference_time=REFERENCE_TIME,
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.UNSUPPORTED


def test_refresh_does_not_write_the_canonical_private_bundle(
    tmp_path: Path,
) -> None:
    """Provider refresh stays pure until credential coordination commits."""
    app_root = tmp_path / "sidekick-usages"
    accounts = app_root / "accounts.json"
    private_root = app_root / "codex"
    codex_home = private_root / "codex-pro"
    tree = PrivateCredentialTree(private_root, account_path=accounts)
    tree.write_bundle(
        codex_home,
        {
            "auth.json": json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "future_metadata": {"preserve": True},
                    "tokens": {
                        "access_token": "access-old",
                        "refresh_token": "refresh-old",
                        "id_token": "id-old",
                        "account_id": "acct_123",
                    },
                }
            ).encode(),
            "config.toml": b'cli_auth_credentials_store = "file"\n',
        },
        expected_bundle_present=False,
        expected_files={"auth.json": None},
    )
    original_bundle = (codex_home / "auth.json").read_bytes()
    account = _account(auth_home=str(codex_home))

    result = CodexProvider(FixedClock()).refresh_credentials(
        authenticated_account(account),
        _RefreshHttp(
            {
                "access_token": _access_token(),
                "refresh_token": "refresh-new",
            }
        ),
    )

    assert isinstance(result, RefreshSuccess)
    assert (codex_home / "auth.json").read_bytes() == original_bundle
    assert account.access_token == "access-old"
    assert isinstance(result.credentials, CodexCredentials)
    assert result.credentials.access_token == _access_token()
    assert result.credentials.refresh_token == "refresh-new"
    assert result.credentials.auth_home == str(codex_home)


def test_private_bundle_preparation_is_pure_and_secret_safe(
    tmp_path: Path,
) -> None:
    """Credential coordination can stage exact bytes without hidden writes."""
    source_home = tmp_path / "source"
    source_home.mkdir()
    (source_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": _access_token(),
                    "refresh_token": "source-refresh",
                    "id_token": "source-id",
                    "account_id": "acct_123",
                },
            }
        )
    )
    account = _account()
    original = account.credentials
    bundle_path = tmp_path / "private" / "codex-pro"

    result = prepare_private_bundle(
        account,
        bundle_path,
        source_home=source_home,
        reference_time=REFERENCE_TIME,
    )

    assert isinstance(result, PreparedCodexAuthBundle)
    assert result.bundle_path == bundle_path
    assert account.credentials is original
    assert not bundle_path.exists()
    assert set(result.file_map()) == {"auth.json", "config.toml"}
    assert "source-id" not in repr(result)


def test_private_preparation_rejects_mismatched_source_material(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "source"
    source_home.mkdir()
    codex_home = tmp_path / "private" / "codex-pro"
    source_blob: JsonObject = {
        "tokens": {
            "access_token": _access_token(account_id="acct_other"),
            "refresh_token": "other-refresh",
            "id_token": "other-id",
        }
    }
    (source_home / "auth.json").write_text(json.dumps(source_blob))

    result = prepare_private_bundle(
        _account(auth_home=str(codex_home)),
        codex_home,
        source_home=source_home,
        reference_time=REFERENCE_TIME,
    )

    assert isinstance(result, ProviderFailure)
    assert result.kind is ProviderFailureKind.IDENTITY_MISMATCH
    assert not codex_home.exists()

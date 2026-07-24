"""Load-bearing credential coordination tests."""

import base64
import json
import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
    DetectedCredentials,
    UsageReport,
)
from sidekick_usages.core.types import AccountLabel, ProviderId, RefreshStatus
from sidekick_usages.credentials import (
    CredentialRefreshSuccess,
    CredentialSaveSuccess,
    CredentialService,
    LocalCredentialSource,
    TokenCredentialSource,
    TokenPromptSpec,
)
from sidekick_usages.credentials.codex import private_codex_home
from sidekick_usages.credentials.refresh import CredentialRefreshCoordinator
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.credential_refresh import (
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.private_credentials import (
    PreparedPrivateBundleWrite,
    PrivateCredentialTree,
)
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
)
from sidekick_usages.providers.codex import CodexProvider
from sidekick_usages.serialization import JsonObject
from sidekick_usages.usage import UsageCheckService
from tests.test_support import (
    REFERENCE_TIME,
    FixedClock,
    make_application_paths,
)

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


def test_private_codex_cache_keys_do_not_collapse_distinct_labels(
    tmp_path: Path,
) -> None:
    """Legacy-equivalent sanitized labels receive distinct durable keys."""
    locations = make_application_paths(tmp_path).private_codex

    first = private_codex_home(locations.canonical, "a b")
    second = private_codex_home(locations.canonical, "a@b")

    assert first != second
    assert first.parent == second.parent == locations.canonical


def _access_token(account_id: str) -> str:
    """Build one JWT-shaped access token with a stable Codex identity."""
    header = {"alg": "none", "typ": "JWT"}
    payload = {
        "exp": 1_900_000_000,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "pro",
        },
    }

    def encode(value: Mapping[str, object]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{encode(header)}.{encode(payload)}.sig"


@pytest.fixture(autouse=True)
def _isolate_default_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent service tests from reading a developer's active login."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-active"))


class _Provider(Provider):
    """Provider double exposing only credential-service boundaries."""

    display_name = "Test provider"
    token_pattern = re.compile(r".+")

    def __init__(
        self,
        provider_id: ProviderId,
        detection: CredentialDetection,
        *,
        refresh: RefreshResult | None = None,
        token_detection: CredentialDetection | None = None,
    ) -> None:
        self.id = provider_id
        self.detection = detection
        self.refresh = refresh
        self.token_detection = token_detection
        self.detected_homes: list[Path | None] = []

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        self.detected_homes.append(credential_home)
        return self.detection

    def credentials_from_token(self, token: str) -> CredentialDetection:
        if self.token_detection is not None:
            return self.token_detection
        if self.id is ProviderId.CLAUDE:
            credentials = ClaudeSetupTokenCredentials(access_token=token)
        else:
            credentials = CodexCredentials(access_token=token)
        return DetectedCredentials(credentials=credentials)

    def _fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        del account, http
        return UsageReport()

    def _refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        del account, http
        if self.refresh is None:
            return ProviderFailure(
                provider_id=self.id,
                kind=ProviderFailureKind.REJECTED,
                message="Test refresh rejected.",
            )
        return self.refresh


class _CodexUsageHttp(HttpClient):
    """Return valid usage while asserting the discovered account header."""

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
        del url
        assert headers["ChatGPT-Account-Id"] == "acct-discovered"
        return {
            "plan_type": "pro",
            "rate_limit": {
                "primary_window": {
                    "used_percent": 25,
                    "reset_at": 1_900_000_000,
                }
            },
        }


def _codex_credentials(
    account_id: str | None,
    *,
    generation: str,
    auth_home: str | None = None,
) -> CodexCredentials:
    return CodexCredentials(
        access_token=f"access-{generation}",
        refresh_token=f"refresh-{generation}",
        account_id=account_id,
        auth_home=auth_home,
        id_token=f"id-{generation}",
        auth_last_refresh=f"2026-07-{generation}T00:00:00Z",
    )


def _account(
    account_id: str | None = "acct-same",
    *,
    auth_home: str | None = None,
) -> Account:
    return Account(
        label=AccountLabel("team"),
        credentials=_codex_credentials(
            account_id,
            generation="01",
            auth_home=auth_home,
        ),
        plan="team",
    )


def _dependencies(
    root: Path,
    accounts: tuple[Account, ...] = (),
) -> tuple[AccountStore, PrivateCredentialTree]:
    paths = make_application_paths(root)
    PersistenceFilesystem(paths.accounts.canonical).repair_parent_permissions()
    private = PrivateCredentialTree(
        paths.private_codex.canonical,
        account_path=paths.accounts.canonical,
        existing_root=paths.private_codex.existing_sidekick,
    )
    store = AccountStore(
        paths.accounts,
        orphaned_credentials_observer=private.observe,
        private_credentials=private,
    ).load()
    for account in accounts:
        store.persist(account)
    return store, private


def _service(
    root: Path,
    provider: Provider,
    accounts: tuple[Account, ...] = (),
) -> tuple[CredentialService, AccountStore, PrivateCredentialTree]:
    store, private = _dependencies(root, accounts)
    http = HttpClient()
    refresh = CredentialRefreshCoordinator(
        store,
        http,
        {provider.id: provider},
        CredentialRefreshTransactions(
            store,
            make_application_paths(root).credential_refresh,
        ),
        clock=FixedClock(),
    )
    service = CredentialService(
        store,
        http,
        {provider.id: provider},
        private,
        clock=FixedClock(),
        refresh_coordinator=refresh,
    )
    return service, store, private


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        (ProviderFailureKind.MISSING, "No local credentials."),
        (ProviderFailureKind.MALFORMED, "Local credentials are malformed."),
    ],
)
def test_source_failures_remain_distinct_and_tokens_are_secret_safe(
    tmp_path: Path,
    kind: ProviderFailureKind,
    message: str,
) -> None:
    failure = ProviderFailure(
        provider_id=ProviderId.CLAUDE,
        kind=kind,
        message=message,
    )
    service, _, _ = _service(
        tmp_path,
        _Provider(ProviderId.CLAUDE, failure),
    )
    secret = "test-only-credential-secret"

    outcome = service.resolve(
        LocalCredentialSource(provider_id=ProviderId.CLAUDE)
    )

    assert outcome == failure
    assert secret not in repr(
        TokenCredentialSource(
            provider_id=ProviderId.CLAUDE,
            token=secret,
        )
    )
    assert "TokenCredentialSource" in repr(
        TokenCredentialSource(
            provider_id=ProviderId.CLAUDE,
            token=secret,
        )
    )


def test_prompt_spec_exposes_only_bounded_token_entry_metadata(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        ProviderId.CLAUDE,
        ProviderFailure(
            provider_id=ProviderId.CLAUDE,
            kind=ProviderFailureKind.MISSING,
            message="No local credentials.",
        ),
    )
    service, store, private = _service(tmp_path, provider)

    spec = service.prompt_spec(ProviderId.CLAUDE)

    assert isinstance(spec, TokenPromptSpec)
    assert spec.provider_id is ProviderId.CLAUDE
    assert spec.display_name == "Test provider"
    assert spec.token_pattern.fullmatch("test-token") is not None
    assert spec.setup_hint is not None
    assert "setup-token claude" in spec.setup_hint
    assert not hasattr(spec, "fetch_usage")

    unavailable = CredentialService(
        store,
        HttpClient(),
        {},
        private,
        clock=FixedClock(),
    ).prompt_spec(ProviderId.CLAUDE)
    assert isinstance(unavailable, ProviderFailure)
    assert unavailable.kind is ProviderFailureKind.UNSUPPORTED


def test_save_rechecks_login_to_setup_authorization_without_cli_preflight(
    tmp_path: Path,
) -> None:
    """The service cannot bypass the shared complete-variant policy."""
    account = Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-shared-material",
            refresh_token="old-refresh-secret",
            access_expiry=KnownExpiry(REFERENCE_TIME),
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile",),
            identity=ClaudeLoginIdentity(
                account_id="old-account",
                organization_id="old-organization",
            ),
        ),
        plan="max",
        last_refresh_at=REFERENCE_TIME,
        last_refresh_status=RefreshStatus.OK,
    )
    service, store, _ = _service(
        tmp_path,
        _Provider(
            ProviderId.CLAUDE,
            ProviderFailure(
                provider_id=ProviderId.CLAUDE,
                kind=ProviderFailureKind.MISSING,
                message="No local credentials.",
            ),
        ),
        (account,),
    )
    source = TokenCredentialSource(
        provider_id=ProviderId.CLAUDE,
        token="sk-ant-oat01-shared-material",
    )
    authority_before = store.path.read_bytes()

    refused = service.save(
        source,
        label=AccountLabel("team"),
        plan=None,
        force=True,
    )

    assert isinstance(refused, ProviderFailure)
    assert refused.kind is ProviderFailureKind.IDENTITY_MISMATCH
    assert store.path.read_bytes() == authority_before

    replaced = service.save(
        source,
        label=AccountLabel("team"),
        plan=None,
        force=True,
        replace_identity=True,
    )

    assert isinstance(replaced, CredentialSaveSuccess)
    saved = store.get("team")
    assert saved is not None
    assert saved.credentials == ClaudeSetupTokenCredentials(
        access_token="sk-ant-oat01-shared-material"
    )
    assert saved.plan == "max"
    assert saved.last_refresh_at is None
    assert saved.last_refresh_status is None


def test_replacing_rejected_setup_token_clears_stale_failure(
    tmp_path: Path,
) -> None:
    """A verified replacement must not retain the previous token's failure."""
    account = Account(
        label=AccountLabel("team"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="sk-ant-oat01-rejected-material"
        ),
        plan="team",
        last_refresh_at=REFERENCE_TIME,
        last_refresh_status=RefreshStatus.FAILED,
        last_refresh_error="Claude rejected the saved setup token.",
    )
    service, store, _ = _service(
        tmp_path,
        _Provider(
            ProviderId.CLAUDE,
            ProviderFailure(
                provider_id=ProviderId.CLAUDE,
                kind=ProviderFailureKind.MISSING,
                message="No local credentials.",
            ),
        ),
        (account,),
    )

    result = service.save(
        TokenCredentialSource(
            provider_id=ProviderId.CLAUDE,
            token="sk-ant-oat01-replacement-material",
        ),
        label=account.label,
        plan=None,
        force=True,
    )

    assert isinstance(result, CredentialSaveSuccess)
    saved = store.get("team")
    assert saved is not None
    assert saved.last_refresh_at is None
    assert saved.last_refresh_status is None
    assert saved.last_refresh_error is None


def test_effective_same_claude_login_preserves_refresh_diagnostic(
    tmp_path: Path,
) -> None:
    """Identity preservation cannot turn a no-op save into a reset."""
    credentials = ClaudeLoginCredentials(
        access_token="same-access-token",
        refresh_token="same-refresh-token",
        access_expiry=KnownExpiry(REFERENCE_TIME),
        refresh_expiry=UnknownExpiry(),
        scopes=("user:profile",),
        identity=ClaudeLoginIdentity(
            account_id="account-id",
            organization_id="organization-id",
        ),
    )
    account = Account(
        label=AccountLabel("team"),
        credentials=credentials,
        plan="team",
        last_refresh_at=REFERENCE_TIME,
        last_refresh_status=RefreshStatus.FAILED,
        last_refresh_error="Provider rejected the saved login.",
    )
    detected = DetectedCredentials(
        credentials=ClaudeLoginCredentials(
            access_token=credentials.access_token,
            refresh_token=credentials.refresh_token,
            access_expiry=credentials.access_expiry,
            refresh_expiry=credentials.refresh_expiry,
            scopes=credentials.scopes,
        ),
        plan="team",
    )
    service, store, _ = _service(
        tmp_path,
        _Provider(ProviderId.CLAUDE, detected),
        (account,),
    )

    result = service.save(
        LocalCredentialSource(provider_id=ProviderId.CLAUDE),
        label=account.label,
        plan=None,
        force=True,
    )

    assert isinstance(result, CredentialSaveSuccess)
    saved = store.get("team")
    assert saved is not None
    assert saved.credentials == credentials
    assert saved.last_refresh_at == REFERENCE_TIME
    assert saved.last_refresh_status is RefreshStatus.FAILED
    assert saved.last_refresh_error == "Provider rejected the saved login."


@pytest.mark.parametrize(
    ("saved_id", "incoming_id", "accepted"),
    [
        ("acct-same", "acct-same", True),
        ("acct-old", "acct-new", False),
        (None, "acct-new", False),
        ("acct-old", None, False),
    ],
)
def test_codex_identity_must_be_proven_before_saved_secrets_are_merged(
    tmp_path: Path,
    saved_id: str | None,
    incoming_id: str | None,
    accepted: bool,
) -> None:
    account = _account(saved_id)
    detected = DetectedCredentials(
        credentials=_codex_credentials(incoming_id, generation="02"),
        plan="pro",
    )
    service, store, private = _service(
        tmp_path,
        _Provider(ProviderId.CODEX, detected),
        (account,),
    )
    authority_before = store.path.read_bytes()

    outcome = service.refresh_from_source(
        "team",
        LocalCredentialSource(provider_id=ProviderId.CODEX),
        replace_identity=False,
    )

    if accepted:
        assert isinstance(outcome, CredentialRefreshSuccess)
        saved = store.get("team")
        assert saved is not None
        assert saved.access_token == "access-02"
        assert saved.refresh_token == "refresh-02"
        assert saved.provider_account_id == "acct-same"
        assert private.observe().value == "present"
    else:
        assert isinstance(outcome, ProviderFailure)
        assert outcome.kind is ProviderFailureKind.IDENTITY_MISMATCH
        assert store.path.read_bytes() == authority_before
        assert store.get("team") == account
        assert not private.root.exists()


def test_explicit_identity_replacement_never_retains_old_codex_secrets(
    tmp_path: Path,
) -> None:
    account = _account("acct-old")
    incoming = _codex_credentials("acct-new", generation="02")
    detected = DetectedCredentials(credentials=incoming, plan="pro")
    service, store, _ = _service(
        tmp_path,
        _Provider(ProviderId.CODEX, detected),
        (account,),
    )

    outcome = service.refresh_from_source(
        "team",
        LocalCredentialSource(provider_id=ProviderId.CODEX),
        replace_identity=True,
    )

    assert isinstance(outcome, CredentialRefreshSuccess)
    saved = store.get("team")
    assert saved is not None
    assert saved.provider_account_id == "acct-new"
    assert saved.refresh_token == "refresh-02"
    assert saved.codex_id_token == "id-02"
    assert saved.codex_last_refresh == "2026-07-02T00:00:00Z"
    assert "refresh-01" not in store.path.read_text()
    assert "id-01" not in store.path.read_text()


def test_local_codex_save_commits_account_and_private_bundle_together(
    tmp_path: Path,
) -> None:
    detected = DetectedCredentials(
        credentials=_codex_credentials("acct-new", generation="02"),
        plan="pro",
    )
    service, store, private = _service(
        tmp_path,
        _Provider(ProviderId.CODEX, detected),
    )

    outcome = service.save(
        LocalCredentialSource(provider_id=ProviderId.CODEX),
        label=AccountLabel("team"),
        plan=None,
        force=False,
    )

    assert isinstance(outcome, CredentialSaveSuccess)
    assert outcome.created
    saved = store.get("team")
    assert saved is not None
    bundle = private_codex_home(private.root, "team")
    assert saved.codex_home == str(bundle)
    auth = private.read_bundle_file(bundle, "auth.json")
    assert auth is not None
    assert json.loads(auth)["tokens"]["account_id"] == "acct-new"


@pytest.mark.parametrize("active_id", ["acct-manual", "acct-other"])
def test_manual_codex_token_never_adopts_the_active_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    active_id: str,
) -> None:
    """Manual tokens persist independently of matching or unrelated logins."""
    active_home = tmp_path / "active-codex"
    active_home.mkdir()
    active_auth = active_home / "auth.json"
    active_payload = {
        "auth_mode": "active-mode",
        "last_refresh": "2026-07-10T10:00:00Z",
        "tokens": {
            "access_token": _access_token(active_id),
            "refresh_token": "active-refresh-secret",
            "id_token": "active-id-secret",
            "account_id": active_id,
        },
    }
    active_bytes = json.dumps(active_payload).encode()
    active_auth.write_bytes(active_bytes)
    monkeypatch.setenv("CODEX_HOME", str(active_home))
    access_token = _access_token("acct-manual")
    detected = DetectedCredentials(
        credentials=CodexCredentials(
            access_token=access_token,
            account_id="acct-manual",
        ),
        plan="pro",
    )
    provider = _Provider(
        ProviderId.CODEX,
        detected,
        token_detection=detected,
    )
    service, store, private = _service(tmp_path, provider)

    outcome = service.save(
        TokenCredentialSource(
            provider_id=ProviderId.CODEX,
            token=access_token,
        ),
        label=AccountLabel("team"),
        plan=None,
        force=False,
    )

    assert isinstance(outcome, CredentialSaveSuccess)
    saved = store.get("team")
    assert saved is not None
    assert saved.codex_home is None
    assert saved.refresh_token is None
    assert saved.codex_id_token is None
    assert private.observe().value == "absent"
    assert active_auth.read_bytes() == active_bytes
    authority = store.path.read_text()
    assert "active-refresh-secret" not in authority
    assert "active-id-secret" not in authority


def test_manual_codex_token_uses_only_its_owned_canonical_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An existing canonical bundle, not CODEX_HOME, completes the token."""
    store, private = _dependencies(tmp_path)
    access_token = _access_token("acct-manual")
    bundle = private_codex_home(private.root, "team")
    account = Account(
        label=AccountLabel("team"),
        credentials=CodexCredentials(
            access_token=access_token,
            refresh_token="canonical-refresh-secret",
            account_id="acct-manual",
            auth_home=str(bundle),
        ),
        plan="pro",
    )
    canonical_auth = json.dumps(
        {
            "auth_mode": "canonical-mode",
            "last_refresh": "2026-07-09T10:00:00Z",
            "tokens": {
                "access_token": access_token,
                "refresh_token": "canonical-refresh-secret",
                "id_token": "canonical-id-secret",
                "account_id": "acct-manual",
            },
        }
    ).encode()
    store.persist_credentials(
        account,
        private_bundle=PreparedPrivateBundleWrite(
            path=bundle,
            files={
                "auth.json": canonical_auth,
                "config.toml": b'cli_auth_credentials_store = "file"\n',
            },
            expected_bundle_present=False,
            expected_files={"auth.json": None, "config.toml": None},
        ),
    )
    active_home = tmp_path / "active-codex"
    active_home.mkdir()
    (active_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "active-mode",
                "last_refresh": "2026-07-10T10:00:00Z",
                "tokens": {
                    "access_token": access_token,
                    "refresh_token": "active-refresh-secret",
                    "id_token": "active-id-secret",
                    "account_id": "acct-manual",
                },
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(active_home))
    detected = DetectedCredentials(
        credentials=CodexCredentials(
            access_token=access_token,
            account_id="acct-manual",
        ),
        plan="pro",
    )
    provider = _Provider(
        ProviderId.CODEX,
        detected,
        token_detection=detected,
    )
    service = CredentialService(
        store,
        HttpClient(),
        {ProviderId.CODEX: provider},
        private,
        clock=FixedClock(),
    )

    outcome = service.save(
        TokenCredentialSource(
            provider_id=ProviderId.CODEX,
            token=access_token,
        ),
        label=AccountLabel("team"),
        plan=None,
        force=False,
    )

    assert isinstance(outcome, CredentialSaveSuccess)
    saved = store.get("team")
    assert saved is not None
    assert saved.codex_id_token == "canonical-id-secret"
    assert saved.codex_last_refresh == "2026-07-09T10:00:00Z"
    persisted_auth = private.read_bundle_file(bundle, "auth.json")
    assert persisted_auth is not None
    persisted = json.loads(persisted_auth)
    assert persisted["auth_mode"] == "canonical-mode"
    assert "active-id-secret" not in store.path.read_text()
    assert b"active-id-secret" not in persisted_auth


def test_codex_usage_identity_discovery_updates_bundle_and_authority(
    tmp_path: Path,
) -> None:
    """Usage self-healing commits one coherent canonical generation."""
    store, private = _dependencies(tmp_path)
    access_token = _access_token("acct-discovered")
    bundle = private_codex_home(private.root, "team")
    account = Account(
        label=AccountLabel("team"),
        credentials=CodexCredentials(
            access_token=access_token,
            refresh_token="saved-refresh",
            id_token="saved-id",
            auth_home=str(bundle),
        ),
        plan="unknown",
    )
    store.persist_credentials(
        account,
        private_bundle=PreparedPrivateBundleWrite(
            path=bundle,
            files={
                "auth.json": json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "last_refresh": "2026-07-09T10:00:00Z",
                        "tokens": {
                            "access_token": access_token,
                            "refresh_token": "saved-refresh",
                            "id_token": "saved-id",
                            "account_id": "acct-discovered",
                        },
                    }
                ).encode(),
                "config.toml": b'cli_auth_credentials_store = "file"\n',
            },
            expected_bundle_present=False,
            expected_files={"auth.json": None, "config.toml": None},
        ),
    )
    clock = FixedClock()
    http = _CodexUsageHttp()
    provider = CodexProvider(clock)
    providers: dict[ProviderId, Provider] = {ProviderId.CODEX: provider}
    credentials = CredentialService(
        store,
        http,
        providers,
        private,
        clock=clock,
    )

    result = UsageCheckService(
        store,
        http,
        providers,
        credentials,
        clock=clock,
    ).check()

    assert result.failures == ()
    assert len(result.usages) == 1
    assert result.usages[0].plan == "pro"
    paths = make_application_paths(tmp_path)
    restored = AccountStore(
        paths.accounts,
        orphaned_credentials_observer=private.observe,
        private_credentials=private,
    ).load()
    saved = restored.get("team")
    assert saved is not None
    assert saved.provider_account_id == "acct-discovered"
    assert saved.plan == "pro"
    assert saved.codex_home == str(bundle)
    auth = private.read_bundle_file(bundle, "auth.json")
    assert auth is not None
    tokens = json.loads(auth)["tokens"]
    assert tokens["account_id"] == "acct-discovered"
    assert tokens["access_token"] == access_token


def test_unreferenced_private_bundle_collision_fails_without_account_write(
    tmp_path: Path,
) -> None:
    detected = DetectedCredentials(
        credentials=_codex_credentials("acct-new", generation="02"),
    )
    service, store, private = _service(
        tmp_path,
        _Provider(ProviderId.CODEX, detected),
    )
    bundle = private_codex_home(private.root, "team")
    private.write_bundle(
        bundle,
        {"auth.json": b'{"tokens":{"account_id":"acct-other"}}'},
        expected_bundle_present=False,
        expected_files={"auth.json": None},
    )
    authority_existed = store.path.exists()

    outcome = service.save(
        LocalCredentialSource(provider_id=ProviderId.CODEX),
        label=AccountLabel("team"),
        plan=None,
        force=False,
    )

    assert isinstance(outcome, ProviderFailure)
    assert outcome.kind is ProviderFailureKind.IDENTITY_MISMATCH
    assert store.get("team") is None
    assert store.path.exists() is authority_existed
    assert b"acct-other" in (bundle / "auth.json").read_bytes()

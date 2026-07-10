"""Load-bearing credential coordination tests."""

import base64
import io
import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest
from rich.console import Console

from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    CodexCredentials,
    DetectedCredentials,
    UsageReport,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials import (
    CredentialRefreshSuccess,
    CredentialSaveSuccess,
    CredentialService,
    LocalCredentialSource,
    TokenCredentialSource,
)
from sidekick_usages.credentials import codex as credential_codex
from sidekick_usages.credentials.codex import private_codex_home
from sidekick_usages.doctor import DoctorService, render_doctor
from sidekick_usages.errors import AuthError
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.maintenance import TokenMaintenanceService
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.artifacts import (
    ExpectedAuthority,
    FileSnapshot,
)
from sidekick_usages.persistence.errors import ReplaceFailedError
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
from sidekick_usages.providers.claude import provider as claude_provider_module
from sidekick_usages.providers.claude.provider import ClaudeProvider
from sidekick_usages.providers.codex import CodexProvider
from sidekick_usages.serialization import JsonObject
from sidekick_usages.usage import UsageCheckService
from tests.test_support import (
    FixedClock,
    make_application_paths,
)

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


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
            credentials = ClaudeCredentials(access_token=token)
        else:
            credentials = CodexCredentials(access_token=token)
        return DetectedCredentials(credentials=credentials)

    def fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        del account, http
        return UsageReport()

    def refresh_credentials(
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
    service = CredentialService(
        store,
        HttpClient(),
        {provider.id: provider},
        private,
        clock=FixedClock(),
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


def test_export_protects_paths_and_publishes_auth_authority_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account = _account("acct-new")
    provider = _Provider(
        ProviderId.CODEX,
        ProviderFailure(
            provider_id=ProviderId.CODEX,
            kind=ProviderFailureKind.MISSING,
            message="No local credentials.",
        ),
    )
    service, _, private = _service(tmp_path, provider, (account,))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-active"))

    protected = service.export_codex("team", private.root / "nested")

    assert isinstance(protected, ProviderFailure)
    assert protected.kind is ProviderFailureKind.UNSUPPORTED

    target = tmp_path / "exported"
    calls: list[str] = []
    original = PersistenceFilesystem.commit_opaque_private

    def fail_auth(
        filesystem: PersistenceFilesystem,
        payload: bytes,
        *,
        expected_source: ExpectedAuthority | None = None,
    ) -> FileSnapshot:
        calls.append(filesystem.authority_path.name)
        if filesystem.authority_path.name == "auth.json":
            raise ReplaceFailedError
        return original(
            filesystem,
            payload,
            expected_source=expected_source,
        )

    monkeypatch.setattr(
        credential_codex.PersistenceFilesystem,
        "commit_opaque_private",
        fail_auth,
    )
    failed = service.export_codex("team", target)

    assert isinstance(failed, ProviderFailure)
    assert failed.kind is ProviderFailureKind.UNREADABLE
    assert calls == ["config.toml", "auth.json"], failed
    assert (target / "config.toml").is_file()
    assert not (target / "auth.json").exists()
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == _PRIVATE_DIRECTORY_MODE
        assert (
            stat.S_IMODE((target / "config.toml").stat().st_mode)
            == _PRIVATE_FILE_MODE
        )
    PersistenceFilesystem(target / "config.toml").read_opaque_private()


def test_provider_secret_never_crosses_persisted_or_doctor_error_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One provider rejection remains secret-safe through every consumer."""
    response_secret = "test-only-provider-response-secret"
    account = Account(
        label=AccountLabel("team"),
        credentials=ClaudeCredentials(
            access_token="sk-ant-oat01-saved-access",
            refresh_token="test-only-saved-refresh",
        ),
        plan="team",
    )
    store, private = _dependencies(tmp_path, (account,))
    clock = FixedClock()
    provider = ClaudeProvider(clock)
    http = HttpClient(clock=clock)
    service = CredentialService(
        store,
        http,
        {ProviderId.CLAUDE: provider},
        private,
        clock=clock,
    )
    monkeypatch.setattr(
        claude_provider_module.shutil,
        "which",
        lambda _name: None,
    )

    def reject_refresh(
        url: str,
        json_body: JsonObject,
        headers: Mapping[str, str] | None = None,
        *,
        operation: HttpOperation,
    ) -> JsonObject:
        del url, json_body, headers, operation
        raise AuthError(response_secret)

    monkeypatch.setattr(http, "post_json", reject_refresh)

    outcome = TokenMaintenanceService(
        store,
        service,
        clock=clock,
    ).refresh_account(account, force=True)
    saved = store.get("team")
    assert saved is not None
    diagnostics = DoctorService(
        tuple(store),
        {ProviderId.CLAUDE: provider},
        {},
        clock,
    ).diagnostics()
    human_output = io.StringIO()
    json_output = io.StringIO()
    render_doctor(
        diagnostics,
        Console(file=human_output, force_terminal=False),
    )
    render_doctor(
        diagnostics,
        Console(file=json_output, force_terminal=False),
        json_output=True,
    )

    assert saved.last_refresh_error == (
        "Claude rejected the credential refresh. Log in again."
    )
    for rendered in (
        repr(outcome),
        store.path.read_text(),
        repr(diagnostics),
        human_output.getvalue(),
        json_output.getvalue(),
    ):
        assert response_secret not in rendered

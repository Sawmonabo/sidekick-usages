"""CLI refresh-flow regression tests."""

import io
import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.console import Console

from sidekick_usages.clock import Clock
from sidekick_usages.core.expiry import Expiry, KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
    DetectedCredentials,
    UsageReport,
    UsageWindow,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials.codex import private_codex_home
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
    RefreshSuccess,
)
from sidekick_usages.providers.claude import (
    ClaudeSetupToken,
    SetupTokenCapture,
    SetupTokenSuccess,
)
from tests.test_support import (
    REFERENCE_TIME,
    CliHarness,
    FixedClock,
    make_account_store_with_private,
    make_app_context,
    make_application_paths,
)

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_default_codex_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prevent CLI tests from reading the developer's active Codex login."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-default"))


class _FakeProvider(Provider):
    """Provider test double with scripted fetch/refresh behavior."""

    id = ProviderId.CLAUDE
    display_name = "Claude Code"
    token_pattern = re.compile(r".+")

    def __init__(
        self,
        fetch_results: Iterable[UsageReport | Exception] = (),
        detected: DetectedCredentials | ProviderFailure | None = None,
        refresh_ok: bool = True,
        provider_id: str = "claude",
        provider_account_id_on_fetch: str | None = None,
    ) -> None:
        """:param fetch_results: Values or exceptions returned in order."""
        self.id = ProviderId(provider_id)
        self.display_name = (
            "Codex CLI" if provider_id == "codex" else "Claude Code"
        )
        self.fetch_results = list(fetch_results)
        self.detected = detected
        self.refresh_ok = refresh_ok
        self.provider_account_id_on_fetch = provider_account_id_on_fetch
        self.fetch_tokens: list[str] = []
        self.refresh_calls = 0
        self.credential_homes: list[Path | None] = []

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        """:return: Scripted detected local credentials."""
        self.credential_homes.append(credential_home)
        if self.detected is not None:
            return self.detected
        return ProviderFailure(
            provider_id=self.id,
            kind=ProviderFailureKind.MISSING,
            message="No test credentials.",
        )

    def credentials_from_token(self, token: str) -> CredentialDetection:
        credentials = (
            ClaudeSetupTokenCredentials(access_token=token)
            if self.id is ProviderId.CLAUDE
            else CodexCredentials(access_token=token)
        )
        return DetectedCredentials(credentials=credentials)

    def fetch_usage(
        self,
        account: Account,
        http: HttpClient,
    ) -> UsageReport:
        """Return or raise the next scripted fetch result."""
        del http
        self.fetch_tokens.append(account.access_token)
        if self.provider_account_id_on_fetch is not None:
            credentials = account.credentials
            assert isinstance(credentials, CodexCredentials)
            account.credentials = replace(
                credentials,
                account_id=self.provider_account_id_on_fetch,
            )
        if not self.fetch_results:
            return _report()
        result = self.fetch_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def refresh_credentials(
        self,
        account: Account,
        http: HttpClient,
    ) -> RefreshResult:
        """Return one scripted immutable credential refresh."""
        del http
        self.refresh_calls += 1
        if not self.refresh_ok:
            return ProviderFailure(
                provider_id=self.id,
                kind=ProviderFailureKind.REJECTED,
                message="Test refresh rejected.",
            )
        credentials = account.credentials
        if isinstance(credentials, CodexCredentials):
            updated = replace(
                credentials,
                access_token="sk-ant-oat01-refreshed",
                refresh_token="refresh-new",
                expiry=KnownExpiry(
                    REFERENCE_TIME.replace(microsecond=0)
                    + timedelta(seconds=60)
                ),
                account_id="acct_refreshed",
            )
        elif isinstance(credentials, ClaudeLoginCredentials):
            updated = replace(
                credentials,
                access_token="sk-ant-oat01-refreshed",
                refresh_token="refresh-new",
                access_expiry=KnownExpiry(
                    REFERENCE_TIME + timedelta(seconds=60)
                ),
            )
        else:
            updated = replace(
                credentials,
                access_token="sk-ant-oat01-refreshed",
            )
        return RefreshSuccess(credentials=updated)


class _SyntheticSetupToken:
    """Return one synthetic setup token without invoking a provider CLI."""

    def __init__(self) -> None:
        self.calls = 0

    def capture_setup_token(self, timeout: int = 600) -> SetupTokenCapture:
        del timeout
        self.calls += 1
        return SetupTokenSuccess("sk-ant-oat01-replacement-setup")


def _report() -> UsageReport:
    """Build a one-window usage report."""
    return UsageReport(
        windows=(UsageWindow(name="5h", utilization=0.1, resets_at=None),),
        plan="team",
    )


def _install_ctx(
    tmp_path: Path,
    provider: _FakeProvider,
    account: Account,
    *,
    clock: Clock | None = None,
) -> tuple[CliHarness, AccountStore, io.StringIO, io.StringIO]:
    """Install an isolated CLI context for refresh-flow tests."""
    store, private = make_account_store_with_private(tmp_path, (account,))
    app_clock = FixedClock() if clock is None else clock
    http = HttpClient()
    providers: dict[ProviderId, Provider] = {provider.id: provider}
    stdout = io.StringIO()
    stderr = io.StringIO()
    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        application=make_app_context(
            store,
            http,
            providers,
            private,
            app_clock,
            heartbeat_providers={},
        ),
    )
    return harness, store, stdout, stderr


def _install_many_ctx(
    tmp_path: Path,
    providers: dict[ProviderId, Provider],
    accounts: Iterable[Account],
    *,
    clock: Clock | None = None,
    claude_setup_token: ClaudeSetupToken | None = None,
) -> tuple[CliHarness, AccountStore, io.StringIO, io.StringIO]:
    """Install an isolated CLI context with multiple saved accounts."""
    store, private = make_account_store_with_private(tmp_path, accounts)
    app_clock = FixedClock() if clock is None else clock
    http = HttpClient()
    stdout = io.StringIO()
    stderr = io.StringIO()
    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        application=make_app_context(
            store,
            http,
            providers,
            private,
            app_clock,
            heartbeat_providers={},
            claude_setup_token=claude_setup_token,
        ),
    )
    return harness, store, stdout, stderr


def _install_empty_ctx(
    tmp_path: Path,
    provider: _FakeProvider,
    *,
    clock: Clock | None = None,
) -> tuple[CliHarness, AccountStore, io.StringIO, io.StringIO]:
    """Install an isolated CLI context with no saved accounts."""
    store, private = make_account_store_with_private(tmp_path)
    app_clock = FixedClock() if clock is None else clock
    http = HttpClient()
    providers: dict[ProviderId, Provider] = {provider.id: provider}
    stdout = io.StringIO()
    stderr = io.StringIO()
    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        application=make_app_context(
            store,
            http,
            providers,
            private,
            app_clock,
            heartbeat_providers={},
        ),
    )
    return harness, store, stdout, stderr


def _codex_cache_dir(tmp_path: Path) -> Path:
    """Return the injected private Codex root for a test context."""
    return make_application_paths(tmp_path).private_codex.canonical


def _codex_cache_home(tmp_path: Path, label: str = "team") -> Path:
    """Return the deterministic collision-resistant private bundle path."""
    root = make_application_paths(tmp_path).private_codex.canonical
    return private_codex_home(root, label)


def _claude_login_account(
    *,
    access_token: str = "sk-ant-oat01-old",
    refresh_token: str = "refresh-old",
    access_expiry: KnownExpiry,
    plan: str = "team",
) -> Account:
    """Build a complete legacy-representable Claude login fixture."""
    return Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expiry=access_expiry,
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile",),
        ),
        plan=plan,
    )


def _codex_acct(
    *,
    access_token: str = "sk-ant-oat01-old",
    refresh_token: str | None = "refresh-old",
    expiry: Expiry | None = None,
    plan: str = "team",
    codex_home: str | None = None,
    provider_account_id: str | None = None,
    id_token: str | None = None,
    last_refresh: str | None = None,
) -> Account:
    """Build a Codex account fixture."""
    return Account(
        label=AccountLabel("team"),
        credentials=CodexCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            expiry=expiry or UnknownExpiry(),
            account_id=provider_account_id,
            auth_home=codex_home,
            id_token=id_token,
            auth_last_refresh=last_refresh,
        ),
        plan=plan,
    )


def _detected(
    *,
    access_token: str,
    provider_id: str = "claude",
    refresh_token: str | None = None,
    expiry: Expiry | None = None,
    plan: str = "unknown",
    provider_account_id: str | None = None,
    id_token: str | None = None,
    last_refresh: str | None = None,
) -> DetectedCredentials:
    """Build one provider-compatible detected credential result."""
    provider = ProviderId(provider_id)
    expiry_value = expiry or UnknownExpiry()
    credentials = (
        ClaudeSetupTokenCredentials(access_token=access_token)
        if provider is ProviderId.CLAUDE
        else CodexCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            expiry=expiry_value,
            account_id=provider_account_id,
            id_token=id_token,
            auth_last_refresh=last_refresh,
        )
    )
    return DetectedCredentials(credentials=credentials, plan=plan)


def _seconds(value: int) -> KnownExpiry:
    """Build a strict whole-second expiry fixture."""
    return KnownExpiry(_EPOCH + timedelta(seconds=value))


def test_refresh_requires_explicit_claude_authentication_method_change(
    tmp_path: Path,
) -> None:
    """A local login cannot silently replace one saved setup token."""
    account = Account(
        label=AccountLabel("team"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="sk-ant-oat01-saved-setup"
        ),
        plan="max",
        heartbeat_enabled=True,
    )
    incoming = ClaudeLoginCredentials(
        access_token="sk-ant-oat01-current-login",
        refresh_token="current-refresh",
        access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
        refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=90)),
        scopes=("user:profile", "user:inference"),
    )
    provider = _FakeProvider(
        detected=DetectedCredentials(credentials=incoming, plan="team")
    )
    harness, store, stdout, _ = _install_ctx(tmp_path, provider, account)
    authority_before = store.path.read_bytes()

    refused = harness.invoke(["refresh", "team"])

    assert refused.exit_code == ExitCode.MANUAL_ACTION
    assert store.path.read_bytes() == authority_before

    replaced = harness.invoke(["refresh", "team", "--replace-auth-method"])

    assert replaced.exit_code == ExitCode.SUCCESS
    saved = store.get("team")
    assert saved is not None
    assert saved.credentials == incoming
    assert saved.heartbeat_enabled is True
    assert "Updated 'team' as a Claude subscription login." in (
        stdout.getvalue()
    )


@pytest.mark.parametrize(
    ("incoming_identity", "requires_replacement"),
    [
        (
            ClaudeLoginIdentity(
                account_id="account-saved",
                organization_id="organization-saved",
            ),
            False,
        ),
        (
            ClaudeLoginIdentity(
                account_id="account-other",
                organization_id="organization-other",
            ),
            True,
        ),
        (None, True),
    ],
)
def test_refresh_enforces_claude_login_identity_policy(
    tmp_path: Path,
    incoming_identity: ClaudeLoginIdentity | None,
    *,
    requires_replacement: bool,
) -> None:
    """Login imports distinguish matching, mismatching, and unknown IDs."""
    saved_identity = ClaudeLoginIdentity(
        account_id="account-saved",
        organization_id="organization-saved",
    )
    account = Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-saved-login",
            refresh_token="saved-refresh",
            access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(minutes=30)),
            refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=30)),
            scopes=("saved:scope", "user:profile"),
            identity=saved_identity,
        ),
        plan="team",
    )
    incoming = ClaudeLoginCredentials(
        access_token="sk-ant-oat01-current-login",
        refresh_token="current-refresh",
        access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
        refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=90)),
        scopes=("current:scope", "user:profile"),
        identity=incoming_identity,
    )
    provider = _FakeProvider(
        detected=DetectedCredentials(credentials=incoming, plan="team")
    )
    harness, store, _, _ = _install_ctx(tmp_path, provider, account)
    authority_before = store.path.read_bytes()

    first = harness.invoke(["refresh", "team"])

    if requires_replacement:
        assert first.exit_code == ExitCode.MANUAL_ACTION
        assert store.path.read_bytes() == authority_before
        result = harness.invoke(["refresh", "team", "--replace-identity"])
    else:
        result = first

    assert result.exit_code == ExitCode.SUCCESS
    saved = store.get("team")
    assert saved is not None
    assert saved.credentials == incoming


def test_refresh_requires_identity_flag_for_equal_access_bytes(
    tmp_path: Path,
) -> None:
    """The public CLI cannot bypass known IDs with exact token bytes."""
    shared_access = "test-only-shared-access-material"
    account = Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token=shared_access,
            refresh_token="test-only-saved-refresh",
            access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(minutes=30)),
            refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=30)),
            scopes=("user:profile",),
            identity=ClaudeLoginIdentity(
                account_id="test-only-saved-account",
                organization_id="test-only-saved-organization",
            ),
        ),
        plan="team",
    )
    incoming = ClaudeLoginCredentials(
        access_token=shared_access,
        refresh_token="test-only-incoming-refresh",
        access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
        refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=90)),
        scopes=("user:profile",),
        identity=ClaudeLoginIdentity(
            account_id="test-only-incoming-account",
            organization_id="test-only-incoming-organization",
        ),
    )
    provider = _FakeProvider(
        detected=DetectedCredentials(credentials=incoming, plan="team")
    )
    harness, store, _, _ = _install_ctx(tmp_path, provider, account)
    authority_before = store.path.read_bytes()

    refused = harness.invoke(["refresh", "team"])

    assert refused.exit_code == ExitCode.MANUAL_ACTION
    assert store.path.read_bytes() == authority_before

    replaced = harness.invoke(["refresh", "team", "--replace-identity"])

    assert replaced.exit_code == ExitCode.SUCCESS
    saved = store.get("team")
    assert saved is not None
    assert saved.credentials == incoming


def test_refresh_requires_both_claude_replacement_authorizations(
    tmp_path: Path,
) -> None:
    """Method and stable-identity changes remain independent decisions."""
    account = Account(
        label=AccountLabel("team"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="sk-ant-oat01-saved-setup"
        ),
        plan="max",
    )
    incoming = ClaudeLoginCredentials(
        access_token="sk-ant-oat01-current-login",
        refresh_token="current-refresh",
        access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
        refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=90)),
        scopes=("user:profile",),
        identity=ClaudeLoginIdentity(
            account_id="account-current",
            organization_id="organization-current",
        ),
    )
    provider = _FakeProvider(
        detected=DetectedCredentials(credentials=incoming, plan="team")
    )
    harness, store, _, _ = _install_ctx(tmp_path, provider, account)
    authority_before = store.path.read_bytes()

    for authorization in (
        "--replace-auth-method",
        "--replace-identity",
    ):
        refused = harness.invoke(["refresh", "team", authorization])
        assert refused.exit_code == ExitCode.MANUAL_ACTION
        assert store.path.read_bytes() == authority_before

    replaced = harness.invoke(
        [
            "refresh",
            "team",
            "--replace-auth-method",
            "--replace-identity",
        ]
    )

    assert replaced.exit_code == ExitCode.SUCCESS
    saved = store.get("team")
    assert saved is not None
    assert saved.credentials == incoming


def test_setup_token_requires_both_login_replacement_authorizations(
    tmp_path: Path,
) -> None:
    """Method and stable-identity replacement remain independent."""
    account = Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-stale-access",
            refresh_token="stale-refresh-secret",
            access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
            refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=90)),
            scopes=("stale:scope", "user:profile"),
            identity=ClaudeLoginIdentity(
                account_id="stale-account",
                organization_id="stale-organization",
            ),
        ),
        plan="max",
        last_refresh_at=REFERENCE_TIME,
        last_refresh_status=RefreshStatus.FAILED,
        last_refresh_error="provider rejected refresh",
        heartbeat_enabled=True,
        heartbeat_5h_reset_at=REFERENCE_TIME + timedelta(hours=2),
        heartbeat_targets=("standard",),
        last_heartbeat_at=REFERENCE_TIME,
    )
    provider = _FakeProvider()
    setup_token = _SyntheticSetupToken()
    harness, store, stdout, stderr = _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: provider},
        (account,),
        claude_setup_token=setup_token,
    )
    authority_before = store.path.read_bytes()

    force_only = harness.invoke(
        ["claude", "setup-token", "--label", "team", "--force"]
    )
    assert force_only.exit_code == ExitCode.MANUAL_ACTION
    assert setup_token.calls == 0
    assert store.path.read_bytes() == authority_before

    identity_only = harness.invoke(
        [
            "claude",
            "setup-token",
            "--label",
            "team",
            "--replace-identity",
        ]
    )
    assert identity_only.exit_code == ExitCode.MANUAL_ACTION
    assert setup_token.calls == 0
    assert store.path.read_bytes() == authority_before

    result = harness.invoke(
        [
            "claude",
            "setup-token",
            "--label",
            "team",
            "--force",
            "--replace-identity",
        ]
    )

    assert result.exit_code == ExitCode.SUCCESS
    assert setup_token.calls == 1
    saved = store.get("team")
    assert saved is not None
    assert saved.credentials == ClaudeSetupTokenCredentials(
        access_token="sk-ant-oat01-replacement-setup"
    )
    assert saved.plan == "max"
    assert saved.heartbeat_enabled is True
    assert saved.heartbeat_5h_reset_at == REFERENCE_TIME + timedelta(hours=2)
    assert saved.heartbeat_targets == ("standard",)
    assert saved.last_heartbeat_at == REFERENCE_TIME
    assert saved.last_refresh_at is None
    assert saved.last_refresh_status is None
    assert saved.last_refresh_error is None
    authority = store.path.read_text()
    for stale in (
        "stale-refresh-secret",
        "stale:scope",
        "stale-account",
        "stale-organization",
    ):
        assert stale not in authority
    assert "Updated 'team'." in stdout.getvalue()
    assert (
        "Authentication for 'team' will change from a Claude "
        "subscription login to a setup token."
    ) in re.sub(r"\s+", " ", stderr.getvalue())


@pytest.mark.parametrize(
    "command",
    [
        ("claude", "setup-token"),
        ("setup-token", "claude"),
    ],
)
def test_setup_token_unknown_login_identity_still_requires_authorization(
    tmp_path: Path,
    command: tuple[str, ...],
) -> None:
    """An identity-free historical login cannot prove token equivalence."""
    account = Account(
        label=AccountLabel("historical"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-historical-access",
            refresh_token="historical-refresh-secret",
            access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
            refresh_expiry=UnknownExpiry(),
            scopes=("user:profile",),
        ),
        plan="max",
        heartbeat_enabled=True,
    )
    setup_token = _SyntheticSetupToken()
    harness, store, _, _ = _install_many_ctx(
        tmp_path,
        {ProviderId.CLAUDE: _FakeProvider()},
        (account,),
        claude_setup_token=setup_token,
    )
    authority_before = store.path.read_bytes()

    refused = harness.invoke([*command, "--label", "historical", "--force"])

    assert refused.exit_code == ExitCode.MANUAL_ACTION
    assert setup_token.calls == 0
    assert store.path.read_bytes() == authority_before

    replaced = harness.invoke(
        [
            *command,
            "--label",
            "historical",
            "--force",
            "--replace-identity",
        ]
    )

    assert replaced.exit_code == ExitCode.SUCCESS
    assert setup_token.calls == 1
    saved = store.get("historical")
    assert saved is not None
    assert saved.credentials == ClaudeSetupTokenCredentials(
        access_token="sk-ant-oat01-replacement-setup"
    )
    assert saved.plan == "max"
    assert saved.heartbeat_enabled is True

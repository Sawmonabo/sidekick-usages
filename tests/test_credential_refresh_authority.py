"""Fresh durable-authority credential-refresh tests."""

from pathlib import Path

from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials.models import CredentialRefreshSuccess
from sidekick_usages.credentials.refresh import (
    CredentialRefreshCoordinator,
    CredentialRefreshReason,
)
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.credential_refresh import (
    CredentialRefreshTransactions,
)
from tests.test_credential_refresh_support import (
    CodexRefreshProvider,
    RefreshProvider,
    login_account,
)
from tests.test_support import (
    FixedClock,
    RuntimeCredentialResolver,
    make_account_store,
)


def test_fresh_rotating_authority_replaces_cached_setup_token(
    tmp_path: Path,
) -> None:
    """Durable rotating authority defeats a stale setup-token cache."""
    label = AccountLabel("claude-team")
    stale_store = make_account_store(
        tmp_path,
        (
            Account(
                label=label,
                credentials=ClaudeSetupTokenCredentials(
                    access_token="sk-ant-oat01-setup"
                ),
            ),
        ),
    )
    current_store = make_account_store(tmp_path)
    current_store.persist(login_account())
    provider = RefreshProvider()
    coordinator = CredentialRefreshCoordinator(
        stale_store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        CredentialRefreshTransactions(
            stale_store,
            tmp_path / "credential-refresh",
        ),
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(stale_store),
    )

    result = coordinator.refresh(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    assert isinstance(result, CredentialRefreshSuccess)
    assert len(provider.calls) == 1
    assert provider.calls[0].refresh_token == "refresh-old"


def test_fresh_present_authority_replaces_cached_missing_state(
    tmp_path: Path,
) -> None:
    """A durable account added through another store is refreshable."""
    label = AccountLabel("claude-team")
    stale_store = make_account_store(tmp_path)
    current_store = make_account_store(tmp_path)
    current_store.persist(login_account())
    provider = RefreshProvider()
    coordinator = CredentialRefreshCoordinator(
        stale_store,
        HttpClient(),
        {ProviderId.CLAUDE: provider},
        CredentialRefreshTransactions(
            stale_store,
            tmp_path / "credential-refresh",
        ),
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(stale_store),
    )

    result = coordinator.refresh(
        provider_id=ProviderId.CLAUDE,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    assert isinstance(result, CredentialRefreshSuccess)
    assert len(provider.calls) == 1
    assert provider.calls[0].refresh_token == "refresh-old"


def test_stabilized_provider_replaces_cached_same_label_provider(
    tmp_path: Path,
) -> None:
    """Only the provider selected from stabilized authority may run."""
    label = AccountLabel("shared-team")
    stale_store = make_account_store(
        tmp_path,
        (login_account(str(label)),),
    )
    current_store = make_account_store(tmp_path)
    current_store.persist(
        Account(
            label=label,
            credentials=CodexCredentials(
                access_token="codex-access-old",
                refresh_token="codex-refresh-old",
            ),
            plan="pro",
        )
    )
    claude = RefreshProvider()
    codex = CodexRefreshProvider()
    coordinator = CredentialRefreshCoordinator(
        stale_store,
        HttpClient(),
        {
            ProviderId.CLAUDE: claude,
            ProviderId.CODEX: codex,
        },
        CredentialRefreshTransactions(
            stale_store,
            tmp_path / "credential-refresh",
        ),
        clock=FixedClock(),
        resolver=RuntimeCredentialResolver(stale_store),
    )

    result = coordinator.refresh(
        provider_id=ProviderId.CODEX,
        label=label,
        reason=CredentialRefreshReason.OPERATOR_FORCED,
    )

    assert isinstance(result, CredentialRefreshSuccess)
    assert claude.calls == []
    assert len(codex.calls) == 1
    assert codex.calls[0].provider_id is ProviderId.CODEX

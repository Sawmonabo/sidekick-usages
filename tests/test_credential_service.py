"""Load-bearing provider-neutral credential coordination tests."""

import re
from pathlib import Path

import pytest

from sidekick_usages.core.models import (
    Account,
    ClaudeSetupTokenCredentials,
    DetectedCredentials,
    UsageReport,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.credentials.authorities import credential_resolver_for
from sidekick_usages.credentials.models import (
    LocalCredentialSource,
    TokenCredentialSource,
    TokenPromptSpec,
)
from sidekick_usages.credentials.refresh import CredentialRefreshCoordinator
from sidekick_usages.credentials.service import CredentialService
from sidekick_usages.http.client import HttpClient
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.credentials.refresh.service import (
    CredentialRefreshTransactions,
)
from sidekick_usages.persistence.filesystem.service import (
    PersistenceFilesystem,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderFailure,
    ProviderFailureKind,
    RefreshResult,
)
from tests.test_support import (
    FixedClock,
    make_application_paths,
)


class _Provider(Provider):
    """Claude provider double for credential-service boundaries."""

    id = ProviderId.CLAUDE
    display_name = "Test provider"
    token_pattern = re.compile(r".+")

    def __init__(
        self,
        detection: CredentialDetection,
        *,
        token_detection: CredentialDetection | None = None,
    ) -> None:
        self.detection = detection
        self.token_detection = token_detection

    def detect_credentials(
        self,
        credential_home: Path | None = None,
    ) -> CredentialDetection:
        del credential_home
        return self.detection

    def credentials_from_token(self, token: str) -> CredentialDetection:
        if self.token_detection is not None:
            return self.token_detection
        return DetectedCredentials(
            credentials=ClaudeSetupTokenCredentials(access_token=token)
        )

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
        return ProviderFailure(
            provider_id=self.id,
            kind=ProviderFailureKind.REJECTED,
            message="Test refresh rejected.",
        )


def _service(
    root: Path,
    provider: Provider,
    accounts: tuple[Account, ...] = (),
) -> tuple[CredentialService, AccountStore]:
    paths = make_application_paths(root)
    PersistenceFilesystem(paths.accounts).repair_parent_permissions()
    private = PrivateCredentialTree(
        paths.private_credentials,
        account_path=paths.accounts,
    )
    store = AccountStore(paths.accounts, private).load()
    for account in accounts:
        store.persist(account)
    http = HttpClient()
    refresh = CredentialRefreshCoordinator(
        store,
        http,
        {provider.id: provider},
        CredentialRefreshTransactions(store, paths.credential_refresh),
        clock=FixedClock(),
        resolver=credential_resolver_for(store, private),
    )
    return (
        CredentialService(
            store,
            http,
            {provider.id: provider},
            refresh_coordinator=refresh,
        ),
        store,
    )


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
    service, _ = _service(tmp_path, _Provider(failure))
    secret = "test-only-credential-secret"

    outcome = service.resolve(
        LocalCredentialSource(provider_id=ProviderId.CLAUDE)
    )

    assert outcome == failure
    source = TokenCredentialSource(
        provider_id=ProviderId.CLAUDE,
        token=secret,
    )
    assert secret not in repr(source)
    assert "TokenCredentialSource" in repr(source)


def test_prompt_spec_exposes_only_bounded_token_entry_metadata(
    tmp_path: Path,
) -> None:
    provider = _Provider(
        ProviderFailure(
            provider_id=ProviderId.CLAUDE,
            kind=ProviderFailureKind.MISSING,
            message="No local credentials.",
        )
    )
    service, store = _service(tmp_path, provider)

    spec = service.prompt_spec(ProviderId.CLAUDE)

    assert isinstance(spec, TokenPromptSpec)
    assert spec.provider_id is ProviderId.CLAUDE
    assert spec.display_name == "Test provider"
    assert spec.token_pattern.fullmatch("test-token") is not None
    assert spec.setup_hint is not None
    assert "claude setup-token" in spec.setup_hint
    unavailable = CredentialService(
        store,
        HttpClient(),
        {},
    ).prompt_spec(ProviderId.CLAUDE)
    assert isinstance(unavailable, ProviderFailure)
    assert unavailable.kind is ProviderFailureKind.UNSUPPORTED

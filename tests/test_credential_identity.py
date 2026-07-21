"""Stable-identity authority regressions for credential replacement."""

from dataclasses import replace
from pathlib import Path

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    DetectedCredentials,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from sidekick_usages.credentials import (
    CredentialRefreshSuccess,
    LocalCredentialSource,
)
from sidekick_usages.providers.base import ProviderFailure, ProviderFailureKind
from tests.test_credential_service import (
    _account,
    _codex_credentials,
    _Provider,
    _service,
)
from tests.test_support import REFERENCE_TIME


def test_claude_known_identity_mismatch_overrides_equal_access_bytes(
    tmp_path: Path,
) -> None:
    """Complete stable identities are authoritative over token equality."""
    shared_access = "test-only-shared-access-material"
    account = Account(
        label=AccountLabel("team"),
        credentials=ClaudeLoginCredentials(
            access_token=shared_access,
            refresh_token="test-only-saved-refresh",
            access_expiry=KnownExpiry(REFERENCE_TIME),
            refresh_expiry=UnknownExpiry(),
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
        access_expiry=KnownExpiry(REFERENCE_TIME),
        refresh_expiry=UnknownExpiry(),
        scopes=("user:profile",),
        identity=ClaudeLoginIdentity(
            account_id="test-only-incoming-account",
            organization_id="test-only-incoming-organization",
        ),
    )
    service, store, _ = _service(
        tmp_path,
        _Provider(
            ProviderId.CLAUDE,
            DetectedCredentials(credentials=incoming, plan="team"),
        ),
        (account,),
    )
    authority_before = store.path.read_bytes()

    refused = service.refresh_from_source(
        "team",
        LocalCredentialSource(provider_id=ProviderId.CLAUDE),
        replace_identity=False,
    )

    assert isinstance(refused, ProviderFailure)
    assert refused.kind is ProviderFailureKind.IDENTITY_MISMATCH
    assert store.path.read_bytes() == authority_before

    replaced = service.refresh_from_source(
        "team",
        LocalCredentialSource(provider_id=ProviderId.CLAUDE),
        replace_identity=True,
    )

    assert isinstance(replaced, CredentialRefreshSuccess)
    saved = store.get("team")
    assert saved is not None
    assert saved.credentials == incoming


def test_codex_known_identity_mismatch_overrides_equal_access_bytes(
    tmp_path: Path,
) -> None:
    """Codex account IDs outrank equality of copied access material."""
    account = _account("acct-saved")
    incoming = replace(
        _codex_credentials("acct-incoming", generation="02"),
        access_token=account.access_token,
    )
    service, store, private = _service(
        tmp_path,
        _Provider(
            ProviderId.CODEX,
            DetectedCredentials(credentials=incoming, plan="pro"),
        ),
        (account,),
    )
    authority_before = store.path.read_bytes()

    outcome = service.refresh_from_source(
        "team",
        LocalCredentialSource(provider_id=ProviderId.CODEX),
        replace_identity=False,
    )

    assert isinstance(outcome, ProviderFailure)
    assert outcome.kind is ProviderFailureKind.IDENTITY_MISMATCH
    assert store.path.read_bytes() == authority_before
    assert not private.root.exists()

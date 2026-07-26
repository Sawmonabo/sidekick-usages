"""Reusable scriptable account-selection fakes."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sidekick_usages.cli.dashboard.models.use import UseActivationSuccess
from sidekick_usages.core.accounts.models import (
    ClaudeAccountAuthority,
    ClaudeManagedLoginAuthority,
    SavedAccount,
)
from sidekick_usages.core.accounts.types import (
    AuthorityGeneration,
    AuthorityId,
    CredentialAction,
    CredentialHealth,
    ProviderIdentity,
    SidekickAccountId,
)
from sidekick_usages.core.types import AccountLabel, ProviderId
from tests.fakes.dashboard.state import (
    CLAUDE_ACTIVE_ACCOUNT_ID,
    CODEX_SAVED_ACCOUNT_ID,
    saved_codex_account,
)

_CODEX_AUTHORITY_ID = AuthorityId(
    "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
)
_CODEX_LOGIN_REQUIRED_ACCOUNT_ID = SidekickAccountId(
    "22222222-2222-4222-8222-222222222222"
)
_CODEX_LOGIN_REQUIRED_AUTHORITY_ID = AuthorityId(
    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
)
_CLAUDE_AUTHORITY_ID = AuthorityId(
    "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
)


@dataclass(slots=True)
class RecordingUseActivation:
    """Record only the sanitized supervisor activation contract."""

    calls: list[tuple[ProviderId, SidekickAccountId, bool]] = field(
        default_factory=list
    )

    def __call__(
        self,
        provider_id: ProviderId,
        account_id: SidekickAccountId,
        allow_remote_control_disconnect: bool,
    ) -> UseActivationSuccess:
        self.calls.append(
            (
                provider_id,
                account_id,
                allow_remote_control_disconnect,
            )
        )
        return UseActivationSuccess()


def scriptable_use_accounts(
    reference_time: datetime,
) -> tuple[SavedAccount, SavedAccount, SavedAccount]:
    """Build healthy Codex and Claude accounts plus one login repair."""
    observed_at = reference_time - timedelta(hours=2)
    codex = saved_codex_account(
        CODEX_SAVED_ACCOUNT_ID,
        _CODEX_AUTHORITY_ID,
        "shared",
        "synthetic-codex-valid",
        observed_at,
    )
    claude = SavedAccount(
        account_id=CLAUDE_ACTIVE_ACCOUNT_ID,
        label=AccountLabel("shared"),
        provider_id=ProviderId.CLAUDE,
        plan="max",
        authority=ClaudeAccountAuthority(
            subscription=ClaudeManagedLoginAuthority(
                authority_id=_CLAUDE_AUTHORITY_ID,
                provider_identity=ProviderIdentity(
                    "synthetic-claude-managed"
                ),
                generation=AuthorityGeneration("claude-generation"),
                access_expires_at=reference_time + timedelta(hours=5),
                refresh_expires_at=reference_time + timedelta(days=90),
                verified_at=observed_at,
                executable_version="2.1.220",
                health=CredentialHealth.HEALTHY,
                action=CredentialAction.NONE,
            )
        ),
        credential_health=CredentialHealth.HEALTHY,
    )
    needs_login = saved_codex_account(
        _CODEX_LOGIN_REQUIRED_ACCOUNT_ID,
        _CODEX_LOGIN_REQUIRED_AUTHORITY_ID,
        "needs-login",
        "synthetic-codex-conflict",
        observed_at,
        credential_health=CredentialHealth.LOGIN_REQUIRED,
    )
    return codex, claude, needs_login

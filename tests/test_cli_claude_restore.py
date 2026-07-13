"""Targeted Claude setup-token restoration CLI contract."""

import io
import json
import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import pytest
from rich.console import Console

from sidekick_usages.core.expiry import KnownExpiry
from sidekick_usages.core.models import (
    Account,
    ClaudeLoginCredentials,
    ClaudeLoginIdentity,
    ClaudeSetupTokenCredentials,
    CodexCredentials,
)
from sidekick_usages.core.types import (
    AccountLabel,
    ExitCode,
    HeartbeatStatus,
    ProviderId,
    RefreshStatus,
)
from sidekick_usages.credentials import ClaudeSetupTokenRestorePreview
from sidekick_usages.credentials.codex import private_codex_home
from sidekick_usages.errors import AuthError, TransientError, UsageError
from sidekick_usages.http import HttpClient, HttpOperation
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.artifacts import AuthorityExpectation
from sidekick_usages.persistence.filesystem import PersistenceFilesystem
from sidekick_usages.persistence.private_credentials import (
    PreparedPrivateBundleWrite,
)
from sidekick_usages.providers.claude import ClaudeProvider
from sidekick_usages.serialization import JsonObject
from tests.test_support import (
    REFERENCE_TIME,
    CliHarness,
    FixedClock,
    make_account_store_with_private,
    make_app_context,
    make_application_paths,
)


class _RestoreUsageHttp(HttpClient):
    """Record the real Claude usage probe and return one scripted outcome."""

    def __init__(self, failure: UsageError | None = None) -> None:
        super().__init__()
        self.failure = failure
        self.authorization_headers: list[str] = []

    def post_capture_headers(
        self,
        url: str,
        json_body: JsonObject,
        headers: Mapping[str, str],
        *,
        operation: HttpOperation,
    ) -> dict[str, str]:
        del url, json_body
        assert operation is HttpOperation.CLAUDE_PROBE
        self.authorization_headers.append(headers["Authorization"])
        if self.failure is not None:
            raise self.failure
        return {}


def test_restore_setup_token_replaces_only_the_exact_claude_credential(
    tmp_path: Path,
) -> None:
    """Restore commits one guarded target and preserves every peer source."""
    target = Account(
        label=AccountLabel("team account"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-current-login",
            refresh_token="current-refresh",
            access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
            refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=90)),
            scopes=("user:profile", "user:inference"),
            identity=ClaudeLoginIdentity(
                account_id="current-account",
                organization_id="current-organization",
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
        last_heartbeat_status=HeartbeatStatus.ACTIVE,
    )
    other_claude = Account(
        label=AccountLabel("other-claude"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="sk-ant-oat01-other-setup"
        ),
        plan="team",
    )
    store, private = make_account_store_with_private(
        tmp_path,
        (target, other_claude),
    )
    bundle = private_codex_home(private.root, "codex-pro")
    codex = Account(
        label=AccountLabel("codex-pro"),
        credentials=CodexCredentials(
            access_token="synthetic-codex-access",
            refresh_token="synthetic-codex-refresh",
            account_id="synthetic-codex-account",
            auth_home=str(bundle),
        ),
        plan="pro",
    )
    private_auth = b'{"synthetic":"private-auth-unchanged"}'
    store.persist_credentials(
        codex,
        private_bundle=PreparedPrivateBundleWrite(
            path=bundle,
            files={"auth.json": private_auth},
            expected_bundle_present=False,
            expected_files={"auth.json": None},
        ),
    )
    before = {str(account.label): account for account in store}
    paths = make_application_paths(tmp_path)
    prototype_bytes = (
        json.dumps(
            {
                "team account": {
                    "token": "sk-ant-oat01-restored-setup",
                    "plan": "legacy-plan-must-not-win",
                },
                "other-legacy": {
                    "token": "sk-ant-oat01-other-legacy",
                    "plan": "team",
                },
            },
            indent=2,
        )
        + "\n"
    ).encode()
    prototype = PersistenceFilesystem(paths.accounts.prototype_cc_usage)
    prototype.commit_opaque_private(
        prototype_bytes,
        expected_source=AuthorityExpectation.ABSENT,
    )
    if os.name != "nt":
        paths.accounts.prototype_cc_usage.parent.chmod(0o755)
    stdout = io.StringIO()
    stderr = io.StringIO()
    http = _RestoreUsageHttp()
    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        application=make_app_context(
            store,
            http,
            {ProviderId.CLAUDE: ClaudeProvider(FixedClock())},
            private,
            FixedClock(),
            heartbeat_providers={},
        ),
    )

    result = harness.invoke(
        ["claude", "restore-setup-token", "team account", "--yes"]
    )

    assert result.exit_code == ExitCode.SUCCESS
    reopened = AccountStore(
        paths.accounts,
        orphaned_credentials_observer=private.observe,
        private_credentials=private,
    ).load()
    assert [str(account.label) for account in reopened] == [
        "team account",
        "other-claude",
        "codex-pro",
    ]
    restored = reopened.get("team account")
    assert restored is not None
    assert restored.credentials == ClaudeSetupTokenCredentials(
        access_token="sk-ant-oat01-restored-setup"
    )
    assert restored.plan == "max"
    assert restored.heartbeat_enabled is True
    assert restored.heartbeat_5h_reset_at == (
        REFERENCE_TIME + timedelta(hours=2)
    )
    assert restored.heartbeat_targets == ("standard",)
    assert restored.last_heartbeat_at == REFERENCE_TIME
    assert restored.last_heartbeat_status is HeartbeatStatus.ACTIVE
    assert restored.last_refresh_at is None
    assert restored.last_refresh_status is None
    assert restored.last_refresh_error is None
    assert reopened.get("other-claude") == before["other-claude"]
    assert reopened.get("codex-pro") == before["codex-pro"]
    assert private.read_bundle_file(bundle, "auth.json") == private_auth
    assert paths.accounts.prototype_cc_usage.read_bytes() == prototype_bytes
    assert http.authorization_headers == ["Bearer sk-ant-oat01-restored-setup"]
    rendered = stdout.getvalue() + stderr.getvalue()
    assert "Restored 'team account' as a Claude setup token." in rendered
    assert "sk-ant-oat01" not in rendered
    assert "current-account" not in rendered
    assert "current-organization" not in rendered


@pytest.mark.parametrize(
    "failure",
    [
        AuthError("The provider rejected the candidate setup token."),
        TransientError("The provider verification is unavailable."),
    ],
    ids=["rejected", "unavailable"],
)
def test_restore_surfaces_verification_failure_before_persistence(
    tmp_path: Path,
    failure: UsageError,
) -> None:
    """A typed verification failure leaves both authorities untouched."""
    target = Account(
        label=AccountLabel("team account"),
        credentials=ClaudeLoginCredentials(
            access_token="sk-ant-oat01-current-login",
            refresh_token="current-refresh",
            access_expiry=KnownExpiry(REFERENCE_TIME + timedelta(hours=1)),
            refresh_expiry=KnownExpiry(REFERENCE_TIME + timedelta(days=90)),
            scopes=("user:profile", "user:inference"),
        ),
        plan="max",
    )
    store, private = make_account_store_with_private(tmp_path, (target,))
    paths = make_application_paths(tmp_path)
    prototype_bytes = (
        json.dumps(
            {
                "team account": {
                    "token": "sk-ant-oat01-candidate-setup",
                    "plan": "legacy-plan-must-not-win",
                }
            },
            indent=2,
        )
        + "\n"
    ).encode()
    PersistenceFilesystem(
        paths.accounts.prototype_cc_usage
    ).commit_opaque_private(
        prototype_bytes,
        expected_source=AuthorityExpectation.ABSENT,
    )
    http = _RestoreUsageHttp(failure)
    application = make_app_context(
        store,
        http,
        {ProviderId.CLAUDE: ClaudeProvider(FixedClock())},
        private,
        FixedClock(),
        heartbeat_providers={},
    )
    preview = application.claude_setup_restore.preview(
        AccountLabel("team account")
    )
    assert isinstance(preview, ClaudeSetupTokenRestorePreview)
    account_bytes = store.path.read_bytes()

    with pytest.raises(type(failure)) as raised:
        application.claude_setup_restore.restore(preview)

    assert raised.value is failure
    assert store.path.read_bytes() == account_bytes
    assert paths.accounts.prototype_cc_usage.read_bytes() == prototype_bytes
    assert http.authorization_headers == [
        "Bearer sk-ant-oat01-candidate-setup"
    ]


def test_restore_cli_renders_rejection_without_persisting(
    tmp_path: Path,
) -> None:
    """The command renders the typed rejection after confirmation."""
    target = Account(
        label=AccountLabel("team account"),
        credentials=ClaudeSetupTokenCredentials(
            access_token="sk-ant-oat01-current-setup"
        ),
        plan="max",
    )
    store, private = make_account_store_with_private(tmp_path, (target,))
    paths = make_application_paths(tmp_path)
    prototype_bytes = (
        b'{\n  "team account": {\n'
        b'    "token": "sk-ant-oat01-candidate-setup",\n'
        b'    "plan": "legacy-plan-must-not-win"\n'
        b"  }\n}\n"
    )
    PersistenceFilesystem(
        paths.accounts.prototype_cc_usage
    ).commit_opaque_private(
        prototype_bytes,
        expected_source=AuthorityExpectation.ABSENT,
    )
    failure_message = "The provider rejected the candidate setup token."
    http = _RestoreUsageHttp(AuthError(failure_message))
    stdout = io.StringIO()
    stderr = io.StringIO()
    harness = CliHarness(
        console=Console(file=stdout, force_terminal=False),
        err_console=Console(file=stderr, force_terminal=False),
        application=make_app_context(
            store,
            http,
            {ProviderId.CLAUDE: ClaudeProvider(FixedClock())},
            private,
            FixedClock(),
            heartbeat_providers={},
        ),
    )
    account_bytes = store.path.read_bytes()

    result = harness.invoke(
        ["claude", "restore-setup-token", "team account", "--yes"]
    )

    assert result.exit_code == ExitCode.MANUAL_ACTION
    assert failure_message in stderr.getvalue()
    assert store.path.read_bytes() == account_bytes
    assert paths.accounts.prototype_cc_usage.read_bytes() == prototype_bytes
    rendered = stdout.getvalue() + stderr.getvalue()
    assert "sk-ant-oat01" not in rendered

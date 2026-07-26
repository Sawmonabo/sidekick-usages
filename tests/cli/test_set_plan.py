"""set-plan command tests."""

import io
from pathlib import Path

from rich.console import Console

from sidekick_usages.core.models import Account, ClaudeSetupTokenCredentials
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.http.client import HttpClient
from sidekick_usages.persistence.accounts.store import AccountStore
from tests.support.application import make_app_context
from tests.support.cli import CliHarness
from tests.support.persistence import (
    make_account_store,
    make_account_store_with_private,
)
from tests.support.time import FixedClock


def _ctx(tmp_path: Path, account: Account) -> tuple[AccountStore, CliHarness]:
    store, private = make_account_store_with_private(tmp_path, (account,))
    http = HttpClient()
    clock = FixedClock()
    harness = CliHarness(
        console=Console(file=io.StringIO(), force_terminal=False),
        err_console=Console(file=io.StringIO(), force_terminal=False),
        application=make_app_context(
            store,
            http,
            {},
            private,
            clock,
            heartbeat_providers={},
        ),
    )
    return store, harness


def _acct(label: str, plan: str) -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=ClaudeSetupTokenCredentials(access_token="t"),
        plan=plan,
    )


def test_set_plan_updates_and_persists(tmp_path: Path) -> None:
    _, harness = _ctx(tmp_path, _acct("acme", "unknown"))

    result = harness.invoke(["set-plan", "acme", "max"])

    assert result.exit_code == 0
    saved = make_account_store(tmp_path).get("acme")
    assert saved is not None
    assert saved.plan == "max"


def test_set_plan_unknown_label_errors(tmp_path: Path) -> None:
    _, harness = _ctx(tmp_path, _acct("acme", "team"))

    result = harness.invoke(["set-plan", "nope", "max"])

    assert result.exit_code == 1


def test_set_plan_rejects_empty_plan(tmp_path: Path) -> None:
    _, harness = _ctx(tmp_path, _acct("acme", "team"))

    result = harness.invoke(["set-plan", "acme", ""])

    assert result.exit_code == 1

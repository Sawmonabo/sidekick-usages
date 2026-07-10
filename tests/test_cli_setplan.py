"""set-plan command tests."""

import io
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages import cli
from sidekick_usages.http import HttpClient
from sidekick_usages.store import Account, AccountStore
from tests._support import FixedClock


def _ctx(tmp_path: Path, account: Account) -> AccountStore:
    store = AccountStore(tmp_path / "accounts.json")
    store.upsert(account)
    store.save()
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers={},
            heartbeat_providers={},
            console=Console(file=io.StringIO(), force_terminal=False),
            err_console=Console(file=io.StringIO(), force_terminal=False),
            clock=FixedClock(),
        )
    )
    return store


def _acct(label: str, plan: str) -> Account:
    return Account(
        label=label, provider_id="claude", access_token="t", plan=plan
    )


def test_set_plan_updates_and_persists(tmp_path):
    _ctx(tmp_path, _acct("acme", "unknown"))

    result = CliRunner().invoke(cli.app, ["set-plan", "acme", "max"])

    assert result.exit_code == 0
    saved = AccountStore(tmp_path / "accounts.json").load().get("acme")
    assert saved is not None
    assert saved.plan == "max"


def test_set_plan_unknown_label_errors(tmp_path):
    _ctx(tmp_path, _acct("acme", "team"))

    result = CliRunner().invoke(cli.app, ["set-plan", "nope", "max"])

    assert result.exit_code == 1


def test_set_plan_rejects_empty_plan(tmp_path):
    _ctx(tmp_path, _acct("acme", "team"))

    result = CliRunner().invoke(cli.app, ["set-plan", "acme", ""])

    assert result.exit_code == 1

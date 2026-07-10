"""set-plan command tests."""

import io
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from sidekick_usages import cli
from sidekick_usages.core.models import Account, ClaudeCredentials
from sidekick_usages.core.types import AccountLabel, ExitCode
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.account_store import AccountStore
from sidekick_usages.persistence.errors import (
    PersistenceError,
    ReplaceFailedError,
    SourceChangedError,
)
from tests.test_support import (
    FixedClock,
    make_account_store,
    make_application_paths,
)


def _ctx(tmp_path: Path, account: Account) -> AccountStore:
    paths = make_application_paths(tmp_path)
    store = make_account_store(tmp_path, (account,))
    cli.set_context(
        cli.AppContext(
            store=store,
            http=HttpClient(),
            providers={},
            heartbeat_providers={},
            private_codex_locations=paths.private_codex,
            lifetime_sources={},
            console=Console(file=io.StringIO(), force_terminal=False),
            err_console=Console(file=io.StringIO(), force_terminal=False),
            clock=FixedClock(),
        )
    )
    return store


def _acct(label: str, plan: str) -> Account:
    return Account(
        label=AccountLabel(label),
        credentials=ClaudeCredentials(access_token="t"),
        plan=plan,
    )


def test_set_plan_updates_and_persists(tmp_path):
    _ctx(tmp_path, _acct("acme", "unknown"))

    result = CliRunner().invoke(cli.app, ["set-plan", "acme", "max"])

    assert result.exit_code == 0
    saved = make_account_store(tmp_path).get("acme")
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


@pytest.mark.parametrize(
    ("error", "expected_exit"),
    [
        (ReplaceFailedError(), ExitCode.SYSTEM_ERROR),
        (SourceChangedError(), ExitCode.MANUAL_ACTION),
    ],
)
def test_entrypoint_preserves_persistence_failure_class(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: PersistenceError,
    expected_exit: ExitCode,
) -> None:
    store = _ctx(tmp_path, _acct("acme", "unknown"))

    def fail_persist(_account: Account) -> None:
        raise error

    monkeypatch.setattr(store, "persist", fail_persist)

    assert cli._run_typer(["set-plan", "acme", "max"]) == expected_exit

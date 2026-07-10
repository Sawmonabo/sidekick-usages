"""Package import and composition-root smoke tests."""

import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from rich.console import Console
from typer.testing import CliRunner

import sidekick_usages
from sidekick_usages import cli
from sidekick_usages.core.models import Account, ClaudeCredentials
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.http import HttpClient
from sidekick_usages.persistence.account_store import AccountStore
from tests.test_support import make_account_store, make_application_paths


def test_package_version_is_set() -> None:
    """``__version__`` is a non-empty string."""
    assert isinstance(sidekick_usages.__version__, str)
    assert sidekick_usages.__version__


def test_failed_composition_closes_initialized_pool_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Transactional composition closes resources without masking failure."""
    client = HttpClient()
    pool = Mock()
    monkeypatch.setattr(client, "_direct_manager", pool)
    monkeypatch.setattr(cli, "HttpClient", lambda *, clock: client)
    monkeypatch.setattr(
        cli,
        "discover_application_paths",
        lambda: make_application_paths(tmp_path),
    )
    failure = RuntimeError("composition sentinel")

    def fail_load(_store: AccountStore) -> None:
        raise failure

    monkeypatch.setattr(cli.AccountStore, "load", fail_load)

    context = cli._build_default_context()
    cli.set_context(context)
    result = CliRunner().invoke(cli.app, ["list"])

    assert result.exception is failure
    pool.clear.assert_called_once_with()


def test_doctor_reads_current_snapshot_without_runtime_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Current-state doctor never constructs the mutable account store."""
    account = Account(
        label=AccountLabel("doctor-account"),
        credentials=ClaudeCredentials(access_token="test-only-access"),
        plan="max",
    )
    make_account_store(tmp_path, (account,))
    paths = make_application_paths(tmp_path)
    monkeypatch.setattr(cli, "discover_application_paths", lambda: paths)
    constructions = 0

    def reject_store(*_args: object, **_kwargs: object) -> None:
        nonlocal constructions
        constructions += 1
        raise AssertionError("doctor constructed AccountStore")

    monkeypatch.setattr(cli, "AccountStore", reject_store)
    context = cli._build_default_context()
    output = io.StringIO()
    context.console = Console(
        file=output,
        force_terminal=False,
        width=200,
    )
    cli.set_context(context)

    result = CliRunner().invoke(cli.app, ["doctor", "--json"])

    assert result.exit_code == 0
    assert constructions == 0
    assert json.loads(output.getvalue())["accounts"][0]["label"] == (
        "doctor-account"
    )

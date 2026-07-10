"""Package import and composition-root smoke tests."""

from unittest.mock import Mock

import pytest

import sidekick_usages
from sidekick_usages import cli
from sidekick_usages.http import HttpClient
from sidekick_usages.store import AccountStore


def test_package_version_is_set() -> None:
    """``__version__`` is a non-empty string."""
    assert isinstance(sidekick_usages.__version__, str)
    assert sidekick_usages.__version__


def test_failed_composition_closes_initialized_pool_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transactional composition closes resources without masking failure."""
    client = HttpClient()
    pool = Mock()
    monkeypatch.setattr(client, "_direct_manager", pool)
    monkeypatch.setattr(cli, "HttpClient", lambda *, clock: client)
    failure = RuntimeError("composition sentinel")

    def fail_load(_store: AccountStore) -> None:
        raise failure

    monkeypatch.setattr(cli.AccountStore, "load", fail_load)

    with pytest.raises(RuntimeError) as exc_info:
        cli._build_default_context()

    assert exc_info.value is failure
    pool.clear.assert_called_once_with()

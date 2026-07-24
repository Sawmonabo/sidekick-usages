"""Application wall-clock contract tests."""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

import sidekick_usages.clock
from sidekick_usages.clock import SystemClock


def test_system_clock_reads_aware_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production clock asks the system for aware UTC."""
    expected = datetime(2026, 6, 12, 12, 34, 56, tzinfo=UTC)
    system_datetime = Mock(spec=datetime)
    system_datetime.now.return_value = expected
    monkeypatch.setattr(sidekick_usages.clock, "datetime", system_datetime)

    assert SystemClock().now() is expected
    system_datetime.now.assert_called_once_with(UTC)

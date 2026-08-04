"""Operating-system process identity tests."""

import os
import sys
from pathlib import Path

import pytest

from sidekick_usages.platform.models import ProcessIdentity
from sidekick_usages.platform.peer import OperatingSystemProcessInspector
from sidekick_usages.platform.types import ProcessLiveness


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="requires Linux procfs semantics",
)
def test_process_inspector_treats_zombie_as_dead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zombie cannot reconnect even while its PID remains present."""
    start_identity = 12345
    process_stat = tmp_path / "stat"
    fields = [b"Z", *([b"0"] * 18), str(start_identity).encode()]
    process_stat.write_bytes(b"42 (sidekick participant) " + b" ".join(fields))
    system_open = os.open

    def open_process_stat(_path: Path, flags: int) -> int:
        return system_open(process_stat, flags)

    monkeypatch.setattr(
        "sidekick_usages.platform.peer.os.open",
        open_process_stat,
    )
    identity = ProcessIdentity(os.getpid(), start_identity)

    assert (
        OperatingSystemProcessInspector().inspect(identity)
        is ProcessLiveness.DEAD
    )

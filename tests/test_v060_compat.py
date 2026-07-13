"""Released-v0.6.0 reader/writer compatibility gates."""

import importlib.util
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from sidekick_usages.serialization import JsonObject, decode_json_object

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = REPO_ROOT / "packaging" / "verify_v060_compat.py"
EXPECTED_CYCLES = (1, 2)
EXPECTED_DIGEST_COUNT = len(EXPECTED_CYCLES) * 3
SPEC = importlib.util.spec_from_file_location(
    "verify_v060_compat", HARNESS_PATH
)
assert SPEC is not None
assert SPEC.loader is not None
verify_v060_compat = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = verify_v060_compat
sys.path.insert(0, str(HARNESS_PATH.parent))
try:
    SPEC.loader.exec_module(verify_v060_compat)
finally:
    sys.path.remove(str(HARNESS_PATH.parent))


def _failed_process(
    argv: tuple[str, ...],
    _cwd: Path,
    _env: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        argv,
        returncode=7,
        stdout="synthetic secret output",
        stderr="synthetic native failure",
    )


def _invalid_oracle_response(
    argv: tuple[str, ...],
    _cwd: Path,
    _env: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        argv,
        returncode=0,
        stdout='{"unexpected":true}',
        stderr="",
    )


def test_command_failure_is_closed_without_native_output(
    tmp_path: Path,
) -> None:
    """A failed Git/archive command cannot become compatibility evidence."""
    oracle = verify_v060_compat.ReleasedV060Oracle(
        tmp_path,
        runner=_failed_process,
    )

    with pytest.raises(
        verify_v060_compat.CompatibilityHarnessError
    ) as exc_info:
        oracle.materialize(tmp_path / "release")

    assert str(exc_info.value) == (
        "A compatibility subprocess rejected the test state."
    )
    assert "secret" not in str(exc_info.value)
    assert "native" not in str(exc_info.value)


def test_invalid_old_oracle_response_is_never_trusted(tmp_path: Path) -> None:
    """A successful process still needs a complete typed oracle response."""
    oracle = verify_v060_compat.ReleasedV060Oracle(
        tmp_path,
        runner=_invalid_oracle_response,
    )
    mutation = verify_v060_compat.OldMutation(
        "claude-max-1",
        verify_v060_compat.OldMutationField.LAST_REFRESH_ERROR,
        "mutated",
    )

    with pytest.raises(
        verify_v060_compat.CompatibilityHarnessError,
        match="released oracle returned an invalid response",
    ):
        oracle.mutate(
            tmp_path / "released-src",
            tmp_path / "accounts.json",
            "0" * 64,
            mutation,
            tmp_path,
        )


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="the release compatibility gate runs in the dedicated Linux job",
)
def test_actual_v060_store_survives_two_current_transform_cycles() -> None:
    """The actual released store preserves latest state across both cycles."""
    result = subprocess.run(
        [
            sys.executable,
            str(HARNESS_PATH),
            "--repository",
            str(REPO_ROOT),
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
        capture_output=True,
        check=False,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    report = decode_json_object(result.stdout.encode("utf-8"))
    assert report["released_commit"] == (
        "6a413b2772c3c11e9ef45a78a06ab79bfc0ca44c"
    )
    assert report["account_order"] == [
        "claude-max-1",
        "codex-plus-1",
        "claude-setup-1",
    ]
    assert report["provider_order"] == ["claude", "codex", "claude"]
    assert report["empty_heartbeat_preflight"] == "rejected"
    assert report["setup_token_round_trip"] == (
        "user:inference_reconstructs_setup_token"
    )
    assert report["advisory_metadata_loss"] == (
        "identity_and_refresh_expiry_only"
    )
    cycle_values = report["cycles"]
    assert isinstance(cycle_values, list)
    cycles: list[JsonObject] = []
    for cycle in cycle_values:
        assert isinstance(cycle, dict)
        cycles.append(cycle)
    assert len(cycles) == len(EXPECTED_CYCLES)

    numbers: list[int] = []
    digests: set[str] = set()
    for cycle in cycles:
        number = cycle["number"]
        assert type(number) is int
        numbers.append(number)
        for field in (
            "version_two_sha256",
            "reverse_v060_sha256",
            "final_state_sha256",
        ):
            digest = cycle[field]
            assert isinstance(digest, str)
            digests.add(digest)
    assert numbers == list(EXPECTED_CYCLES)
    assert len(digests) == EXPECTED_DIGEST_COUNT
    assert all(
        isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        for digest in digests
    )

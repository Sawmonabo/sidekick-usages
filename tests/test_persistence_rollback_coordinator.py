"""Persistence rollback coordinator checkpoint tests."""

from pathlib import Path

import pytest

from sidekick_usages.persistence.artifacts import (
    AuthorityGeneration,
    sha256_digest,
)
from sidekick_usages.persistence.errors import (
    PersistenceCode,
    PersistenceError,
    RollbackCompatibilityError,
)
from sidekick_usages.persistence.migrations.errors import (
    ReleasedVerifierBoundaryError,
    VerificationPhase,
)
from sidekick_usages.persistence.schemas import (
    decode_generation_zero,
    encode_version_two,
)
from sidekick_usages.persistence.transforms import accounts_to_version_two
from tests.test_persistence_coordinator import (
    EXPECTED_SCHEDULER_CHECKS,
    GENERATION_ZERO,
    VERSION_TWO,
    RecordingVerifier,
    _account,
    _service,
)


@pytest.mark.parametrize("checkpoint", ["current", "snapshot", "committed"])
def test_rollback_resumes_snapshot_and_committed_checkpoints(
    tmp_path: Path,
    checkpoint: str,
) -> None:
    """Every rollback checkpoint converges and reports its exact snapshot."""
    payload = GENERATION_ZERO if checkpoint == "committed" else VERSION_TWO
    service, authority, _prototype, log, _scheduler, verifier = _service(
        tmp_path,
        payload,
    )
    snapshot_artifact = None
    if checkpoint in {"snapshot", "committed"}:
        snapshot_artifact = authority.seed_immutable(
            AuthorityGeneration.VERSION_TWO,
            VERSION_TWO,
        )
    if checkpoint == "committed":
        decoy_payload = encode_version_two(
            accounts_to_version_two((_account("backup", targets=()),))
        )
        decoy = authority.seed_immutable(
            AuthorityGeneration.VERSION_TWO,
            decoy_payload,
        )
        assert snapshot_artifact is not None
        assert decoy.basename < snapshot_artifact.basename

    result = service.prepare_rollback()

    assert result.code is PersistenceCode.ROLLBACK_PREPARED
    assert result.assessment.code is PersistenceCode.ROLLBACK_PREPARED
    assert authority.snapshot is not None
    assert authority.snapshot.data == GENERATION_ZERO
    assert verifier.preflight_calls == EXPECTED_SCHEDULER_CHECKS
    assert verifier.verified[-1] == authority.snapshot
    expected_basename = (
        snapshot_artifact.basename
        if snapshot_artifact is not None
        else authority.grammar.backup_basename(
            AuthorityGeneration.VERSION_TWO,
            sha256_digest(VERSION_TWO),
        )
    )
    assert result.artifact_basename == expected_basename
    if checkpoint == "committed":
        assert not any(entry.startswith("commit:") for entry in log)


@pytest.mark.parametrize("failure", ["schema", "preflight", "verify"])
def test_rollback_failures_preserve_their_exact_commit_boundary(
    tmp_path: Path,
    failure: str,
) -> None:
    """Preflight failures do not snapshot; post-proof failure stays typed."""
    payload = (
        encode_version_two(
            accounts_to_version_two(
                (_account("claude-empty-targets", targets=()),)
            )
        )
        if failure == "schema"
        else VERSION_TWO
    )
    verifier = RecordingVerifier()
    if failure == "preflight":
        verifier.preflight_error = RuntimeError("raw oracle failure")
    elif failure == "verify":
        verifier.verify_error = RuntimeError("raw reader failure")
    service, authority, _prototype, log, _scheduler, _ = _service(
        tmp_path,
        payload,
        verifier=verifier,
    )
    before = authority.snapshot

    if failure == "schema":
        error_type: type[PersistenceError] = RollbackCompatibilityError
    else:
        error_type = ReleasedVerifierBoundaryError
    with pytest.raises(error_type) as exc_info:
        service.prepare_rollback()

    if failure in {"schema", "preflight"}:
        assert authority.snapshot == before
        assert authority.managed == {}
        assert log == []
    else:
        assert isinstance(exc_info.value, ReleasedVerifierBoundaryError)
        assert exc_info.value.phase is VerificationPhase.POST_COMMIT
        assert exc_info.value.code is PersistenceCode.DURABILITY_UNCERTAIN
        assert authority.snapshot is not None
        decode_generation_zero(authority.snapshot.data)

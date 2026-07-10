#!/usr/bin/env python3
"""Verify current account transforms against the released v0.6.0 store."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from sidekick_usages.persistence.errors import RollbackCompatibilityError
from sidekick_usages.persistence.schemas import (
    VersionOneDocument,
    decode_generation_zero,
    decode_version_one,
    encode_generation_zero,
    encode_version_one,
)
from sidekick_usages.persistence.transforms import (
    generation_zero_to_version_one,
    version_one_to_v060,
)
from sidekick_usages.persistence.v060 import PINNED_V060_COMMIT

_ACCOUNT_ORDER = ("claude-max-1", "codex-plus-1")
_PROVIDER_ORDER = ("claude", "codex")
_COMMAND_TIMEOUT_SECONDS = 60
_SHA256_LENGTH = 64

_OLD_ORACLE_PROGRAM = r"""
import hashlib
import json
import sys
from pathlib import Path


def deny_network(event, _args):
    if event.startswith("socket."):
        raise PermissionError("Network access is disabled.")


def digest(state):
    payload = json.dumps(
        state,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


sys.addaudithook(deny_network)
source_root = Path(sys.argv[1]).resolve()
account_file = Path(sys.argv[2]).resolve()
expected_digest = sys.argv[3]
expected_identity = json.loads(sys.argv[4])
label, field, value = sys.argv[5:8]
sys.path.insert(0, str(source_root))

import sidekick_usages.store as released_store

expected_store = source_root / "sidekick_usages" / "store.py"
if Path(released_store.__file__).resolve() != expected_store:
    raise SystemExit(10)

store = released_store.AccountStore(account_file).load()
accounts = list(store)
labels = [account.label for account in accounts]
providers = [account.provider_id for account in accounts]
if [labels, providers] != expected_identity:
    raise SystemExit(11)

before = {
    account.label: account.to_dict()
    for account in accounts
}
before_digest = digest(before)
if before_digest != expected_digest:
    raise SystemExit(12)

if field not in {"last_refresh_error", "last_heartbeat_error"}:
    raise SystemExit(13)
account = store.get(label)
if account is None:
    raise SystemExit(14)
setattr(account, field, value)
store.save()

after_accounts = list(store)
after = {
    account.label: account.to_dict()
    for account in after_accounts
}
print(
    json.dumps(
        {
            "before_sha256": before_digest,
            "after_sha256": digest(after),
            "labels": [account.label for account in after_accounts],
            "providers": [
                account.provider_id for account in after_accounts
            ],
        },
        separators=(",", ":"),
    )
)
"""


class CompatibilityHarnessError(RuntimeError):
    """A safe released-reader compatibility failure."""


class OldMutationField(StrEnum):
    """Released account fields mutated by compatibility cycles."""

    LAST_REFRESH_ERROR = "last_refresh_error"
    LAST_HEARTBEAT_ERROR = "last_heartbeat_error"


@dataclass(frozen=True, slots=True)
class OldMutation:
    """One deterministic mutation performed by the released writer."""

    label: str
    field: OldMutationField
    value: str


@dataclass(frozen=True, slots=True)
class OracleObservation:
    """Secret-free evidence returned by the released subprocess."""

    before_sha256: str
    after_sha256: str
    labels: tuple[str, ...]
    providers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CurrentCycleResult:
    """Deterministic bytes owned by the current pure transform seam."""

    version_one: bytes
    reverse_v060: bytes


@dataclass(frozen=True, slots=True)
class CycleEvidence:
    """Stable evidence for one complete old/current/old cycle."""

    number: int
    version_one_sha256: str
    reverse_v060_sha256: str
    final_state_sha256: str


@dataclass(frozen=True, slots=True)
class CompatibilityReport:
    """Deterministic result of the complete compatibility harness."""

    cycles: tuple[CycleEvidence, ...]

    def json_object(self) -> dict[str, object]:
        """Return the stable machine-readable report."""
        return {
            "released_commit": PINNED_V060_COMMIT,
            "account_order": list(_ACCOUNT_ORDER),
            "provider_order": list(_PROVIDER_ORDER),
            "empty_heartbeat_preflight": "rejected",
            "cycles": [
                {
                    "number": cycle.number,
                    "version_one_sha256": cycle.version_one_sha256,
                    "reverse_v060_sha256": cycle.reverse_v060_sha256,
                    "final_state_sha256": cycle.final_state_sha256,
                }
                for cycle in self.cycles
            ],
        }


type ProcessRunner = Callable[
    [tuple[str, ...], Path, Mapping[str, str] | None],
    subprocess.CompletedProcess[str],
]


def run_process(
    argv: tuple[str, ...],
    cwd: Path,
    env: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    """Run one bounded argv-only subprocess."""
    return subprocess.run(
        list(argv),
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )


@dataclass(frozen=True, slots=True)
class ReleasedV060Oracle:
    """Materialize and invoke the actual pinned release store."""

    repository: Path
    runner: ProcessRunner = run_process

    def materialize(self, destination: Path) -> Path:
        """Extract the exact pinned commit from the supplied repository."""
        git = shutil.which("git")
        if git is None:
            raise CompatibilityHarnessError("Git is unavailable.")
        revision = self._command(
            (
                git,
                "-C",
                str(self.repository),
                "rev-parse",
                "--verify",
                f"{PINNED_V060_COMMIT}^{{commit}}",
            ),
            cwd=self.repository,
        )
        if revision.stdout.strip() != PINNED_V060_COMMIT:
            raise CompatibilityHarnessError(
                "The pinned v0.6.0 commit is unavailable."
            )

        archive = destination / "released-v060.tar"
        self._command(
            (
                git,
                "-C",
                str(self.repository),
                "archive",
                "--format=tar",
                f"--output={archive}",
                PINNED_V060_COMMIT,
            ),
            cwd=self.repository,
        )
        source = destination / "released-v060"
        source.mkdir()
        try:
            with tarfile.open(archive, mode="r:") as release_archive:
                release_archive.extractall(source, filter="data")
        except OSError, tarfile.TarError:
            raise CompatibilityHarnessError(
                "The pinned v0.6.0 archive could not be extracted."
            ) from None
        self._verify_release(source)
        return source / "src"

    def mutate(
        self,
        source_root: Path,
        account_file: Path,
        expected_digest: str,
        mutation: OldMutation,
        sandbox_home: Path,
    ) -> OracleObservation:
        """Read and mutate state through the actual released store."""
        identity = json.dumps(
            [_ACCOUNT_ORDER, _PROVIDER_ORDER],
            separators=(",", ":"),
        )
        result = self._command(
            (
                sys.executable,
                "-I",
                "-c",
                _OLD_ORACLE_PROGRAM,
                str(source_root),
                str(account_file),
                expected_digest,
                identity,
                mutation.label,
                mutation.field.value,
                mutation.value,
            ),
            cwd=sandbox_home,
            env=_isolated_environment(sandbox_home),
        )
        return _oracle_observation(result.stdout)

    def _command(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(argv, cwd, env)
        except OSError, subprocess.SubprocessError:
            raise CompatibilityHarnessError(
                "A compatibility subprocess could not complete."
            ) from None
        if result.returncode != 0:
            raise CompatibilityHarnessError(
                "A compatibility subprocess rejected the test state."
            )
        return result

    @staticmethod
    def _verify_release(source: Path) -> None:
        """Confirm the archive carries the expected release metadata."""
        pyproject = source / "pyproject.toml"
        store = source / "src" / "sidekick_usages" / "store.py"
        try:
            metadata = pyproject.read_text(encoding="utf-8")
        except OSError:
            raise CompatibilityHarnessError(
                "The pinned archive is incomplete."
            ) from None
        if 'version = "0.6.0"' not in metadata or not store.is_file():
            raise CompatibilityHarnessError(
                "The pinned archive is not the v0.6.0 source."
            )


class PureCurrentTransform:
    """Replaceable seam around current strict, pure schema transforms."""

    def representative_source(self) -> bytes:
        """Return canonical representative generation-zero bytes."""
        raw = (
            json.dumps(
                _representative_root(),
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        document = decode_generation_zero(raw)
        first = encode_generation_zero(document)
        if encode_generation_zero(document) != first:
            raise CompatibilityHarnessError(
                "Current generation-zero encoding is not deterministic."
            )
        return first

    def verify_empty_collection_preflight(self, source: bytes) -> None:
        """Prove explicit empty heartbeat state is rejected, not coerced."""
        current = generation_zero_to_version_one(
            decode_generation_zero(source)
        )
        first = replace(current.accounts[0], heartbeat_targets=())
        unrepresentable = VersionOneDocument((first, *current.accounts[1:]))
        try:
            version_one_to_v060(unrepresentable)
        except RollbackCompatibilityError:
            if unrepresentable.accounts[0].heartbeat_targets == ():
                return
        raise CompatibilityHarnessError(
            "Explicit empty heartbeat state bypassed rollback preflight."
        )

    def run_cycle(self, source: bytes, number: int) -> CurrentCycleResult:
        """Upgrade, mutate, validate, and reverse one current-code cycle."""
        source_document = decode_generation_zero(source)
        current = generation_zero_to_version_one(source_document)
        target_index = number % len(current.accounts)
        records = tuple(
            replace(record, plan=f"{record.plan}-current-{number}")
            if index == target_index
            else record
            for index, record in enumerate(current.accounts)
        )
        mutated = VersionOneDocument(records)
        version_one = encode_version_one(mutated)
        if encode_version_one(decode_version_one(version_one)) != version_one:
            raise CompatibilityHarnessError(
                "Current version-one encoding is not deterministic."
            )
        reverse_document = version_one_to_v060(decode_version_one(version_one))
        reverse = encode_generation_zero(reverse_document)
        if encode_generation_zero(decode_generation_zero(reverse)) != reverse:
            raise CompatibilityHarnessError(
                "Current rollback encoding is not deterministic."
            )
        return CurrentCycleResult(version_one, reverse)


def run_compatibility(repository: Path) -> CompatibilityReport:
    """Run two deterministic actual-release compatibility cycles."""
    _install_network_guard()
    transform = PureCurrentTransform()
    source = transform.representative_source()
    transform.verify_empty_collection_preflight(source)
    oracle = ReleasedV060Oracle(repository.resolve())
    evidence: list[CycleEvidence] = []

    with tempfile.TemporaryDirectory(
        prefix="sidekick-v060-compat-"
    ) as temporary:
        workspace = Path(temporary)
        released_source = oracle.materialize(workspace)
        sandbox_home = workspace / "home"
        sandbox_home.mkdir()
        account_file = sandbox_home / "accounts.json"

        for number in (1, 2):
            pre_mutation = OldMutation(
                label=_ACCOUNT_ORDER[(number - 1) % 2],
                field=OldMutationField.LAST_REFRESH_ERROR,
                value=f"released-pre-cycle-{number}",
            )
            source = _released_mutation(
                oracle,
                released_source,
                account_file,
                source,
                pre_mutation,
                sandbox_home,
            )

            current = transform.run_cycle(source, number)
            account_file.write_bytes(current.reverse_v060)
            post_mutation = OldMutation(
                label=_ACCOUNT_ORDER[number % 2],
                field=OldMutationField.LAST_HEARTBEAT_ERROR,
                value=f"released-post-cycle-{number}",
            )
            source = _released_mutation(
                oracle,
                released_source,
                account_file,
                current.reverse_v060,
                post_mutation,
                sandbox_home,
            )
            evidence.append(
                CycleEvidence(
                    number=number,
                    version_one_sha256=_sha256(current.version_one),
                    reverse_v060_sha256=_sha256(current.reverse_v060),
                    final_state_sha256=_state_digest(source),
                )
            )

    return CompatibilityReport(tuple(evidence))


def _released_mutation(
    oracle: ReleasedV060Oracle,
    released_source: Path,
    account_file: Path,
    source: bytes,
    mutation: OldMutation,
    sandbox_home: Path,
) -> bytes:
    """Run and verify one actual-release read/write mutation."""
    account_file.write_bytes(source)
    before_digest = _state_digest(source)
    expected_after = _expected_mutation_digest(source, mutation)
    observation = oracle.mutate(
        released_source,
        account_file,
        before_digest,
        mutation,
        sandbox_home,
    )
    result = account_file.read_bytes()
    if (
        observation.before_sha256 != before_digest
        or observation.after_sha256 != expected_after
        or _state_digest(result) != expected_after
        or observation.labels != _ACCOUNT_ORDER
        or observation.providers != _PROVIDER_ORDER
        or _state_identity(result) != (_ACCOUNT_ORDER, _PROVIDER_ORDER)
    ):
        raise CompatibilityHarnessError(
            "The released store did not preserve the expected state."
        )
    return result


def _representative_root() -> dict[str, object]:
    """Return complete synthetic v0.6.0 state for both providers."""
    audit_time = "2026-07-10T12:00:00Z"
    reset_time = "2026-07-11T12:00:00Z"
    return {
        "claude-max-1": {
            "provider_id": "claude",
            "provider_account_id": None,
            "access_token": "test-only-claude-access-token",
            "refresh_token": "test-only-claude-refresh-token",
            "expires_at": 1_783_771_200_000,
            "plan": "max",
            "scopes": ["user:profile", "user:inference"],
            "codex_home": None,
            "codex_id_token": None,
            "codex_last_refresh": None,
            "last_refresh_at": audit_time,
            "last_refresh_status": "ok",
            "last_refresh_error": None,
            "heartbeat_enabled": True,
            "heartbeat_5h_reset_at": reset_time,
            "heartbeat_window_resets": {"five-hour": reset_time},
            "heartbeat_targets": ["five-hour"],
            "last_heartbeat_at": audit_time,
            "last_heartbeat_status": "active",
            "last_heartbeat_error": None,
        },
        "codex-plus-1": {
            "provider_id": "codex",
            "provider_account_id": "acct_test_only",
            "access_token": "test-only-codex-access-token",
            "refresh_token": "test-only-codex-refresh-token",
            "expires_at": 1_783_771_200,
            "plan": "plus",
            "scopes": None,
            "codex_home": "/synthetic/codex/account",
            "codex_id_token": "test-only-codex-id-token",
            "codex_last_refresh": audit_time,
            "last_refresh_at": audit_time,
            "last_refresh_status": "skipped",
            "last_refresh_error": None,
            "heartbeat_enabled": True,
            "heartbeat_5h_reset_at": reset_time,
            "heartbeat_window_resets": {"standard": reset_time},
            "heartbeat_targets": ["standard"],
            "last_heartbeat_at": audit_time,
            "last_heartbeat_status": "warmed",
            "last_heartbeat_error": None,
        },
    }


def _expected_mutation_digest(
    payload: bytes,
    mutation: OldMutation,
) -> str:
    root = _json_object(payload)
    record_value = root.get(mutation.label)
    if not isinstance(record_value, dict):
        raise CompatibilityHarnessError(
            "Representative compatibility state is invalid."
        )
    record: dict[str, object] = {}
    for field_name, field_value in record_value.items():
        if not isinstance(field_name, str):
            raise CompatibilityHarnessError(
                "Representative compatibility state is invalid."
            )
        record[field_name] = field_value
    record[mutation.field.value] = mutation.value
    root[mutation.label] = record
    return _object_digest(root)


def _state_identity(
    payload: bytes,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    document = decode_generation_zero(payload)
    return (
        tuple(str(record.label) for record in document.accounts),
        tuple(record.provider_id.value for record in document.accounts),
    )


def _state_digest(payload: bytes) -> str:
    decode_generation_zero(payload)
    return _object_digest(_json_object(payload))


def _json_object(payload: bytes) -> dict[str, object]:
    try:
        decoded: object = json.loads(payload)
    except UnicodeDecodeError, json.JSONDecodeError:
        raise CompatibilityHarnessError(
            "Compatibility state is not a JSON object."
        ) from None
    if not isinstance(decoded, dict) or not all(
        isinstance(key, str) for key in decoded
    ):
        raise CompatibilityHarnessError(
            "Compatibility state is not a JSON object."
        )
    return {str(key): value for key, value in decoded.items()}


def _object_digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except TypeError, ValueError, UnicodeEncodeError:
        raise CompatibilityHarnessError(
            "Compatibility state cannot be hashed."
        ) from None
    return _sha256(encoded)


def _oracle_observation(payload: str) -> OracleObservation:
    root = _json_object(payload.encode("utf-8"))
    before = root.get("before_sha256")
    after = root.get("after_sha256")
    labels = root.get("labels")
    providers = root.get("providers")
    if (
        not isinstance(before, str)
        or not isinstance(after, str)
        or not _is_sha256(before)
        or not _is_sha256(after)
        or not isinstance(labels, list)
        or not all(isinstance(label, str) for label in labels)
        or not isinstance(providers, list)
        or not all(isinstance(provider, str) for provider in providers)
    ):
        raise CompatibilityHarnessError(
            "The released oracle returned an invalid response."
        )
    return OracleObservation(
        before,
        after,
        tuple(label for label in labels if isinstance(label, str)),
        tuple(provider for provider in providers if isinstance(provider, str)),
    )


def _is_sha256(value: str) -> bool:
    return len(value) == _SHA256_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _isolated_environment(home: Path) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "LC_ALL": "C.UTF-8",
        "NO_PROXY": "*",
        "no_proxy": "*",
        "PYTHONNOUSERSITE": "1",
    }
    for name in ("PATH", "SYSTEMROOT", "WINDIR"):
        if (value := os.environ.get(name)) is not None:
            environment[name] = value
    return environment


def _install_network_guard() -> None:
    def deny_network(event: str, _args: tuple[object, ...]) -> None:
        if event.startswith("socket."):
            raise PermissionError("Network access is disabled.")

    sys.addaudithook(deny_network)


def _default_repository() -> Path:
    return Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify current account transforms against the actual pinned "
            "v0.6.0 reader and writer."
        )
    )
    parser.add_argument(
        "--repository",
        type=Path,
        default=_default_repository(),
        help="Git repository containing the pinned release commit.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the compatibility gate and emit deterministic JSON evidence."""
    arguments = _parser().parse_args(argv)
    try:
        report = run_compatibility(arguments.repository)
    except CompatibilityHarnessError:
        sys.stderr.write(
            "Released v0.6.0 compatibility verification failed.\n"
        )
        return 1
    sys.stdout.write(
        json.dumps(
            report.json_object(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

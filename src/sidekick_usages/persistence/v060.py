"""Offline execution of the exact released v0.6.0 account reader."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from sidekick_usages.persistence.artifacts import FileSnapshot
from sidekick_usages.persistence.errors import (
    PersistenceCode,
    PersistenceError,
)
from sidekick_usages.persistence.schemas import (
    GenerationZeroDocument,
    decode_generation_zero,
)
from sidekick_usages.serialization import decode_json_value

PINNED_V060_COMMIT = "6a413b2772c3c11e9ef45a78a06ab79bfc0ca44c"
V060_READER_BUNDLE_SHA256 = (
    "135953ec42ca61324fb9064cb71f2afa854200c918ef4332fdb09e29d98b2f62"
)

_BUNDLE_RELATIVE_PATH = ("_compat", "v060-reader.zip")
_COMMAND_TIMEOUT_SECONDS = 30
_PREFLIGHT_SENTINEL = "sidekick-v060-reader-ready"
_VERIFY_SENTINEL = "sidekick-v060-reader-verified"

_COMMON_PROGRAM = r"""
import hashlib
import json
import stat
import sys
from pathlib import Path


def deny_network(event, _args):
    if event.startswith("socket."):
        raise PermissionError("Network access is disabled.")


def digest(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def raw_snapshot(path, expected_size):
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size != expected_size
    ):
        raise SystemExit(12)
    with path.open("rb") as stream:
        payload = stream.read(expected_size + 1)
    after = path.lstat()
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if len(payload) != expected_size or before_identity != after_identity:
        raise SystemExit(13)
    return before_identity, hashlib.sha256(payload).hexdigest()


sys.addaudithook(deny_network)
bundle = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(bundle))
import sidekick_usages
import sidekick_usages.store as released_store

module_path = str(released_store.__file__).replace("\\", "/")
expected_suffix = (
    str(bundle).replace("\\", "/") + "/sidekick_usages/store.py"
)
if sidekick_usages.__version__ != "0.6.0" or module_path != expected_suffix:
    raise SystemExit(10)
"""

_PREFLIGHT_PROGRAM = (
    _COMMON_PROGRAM + f'\nsys.stdout.write("{_PREFLIGHT_SENTINEL}\\n")\n'
)

_VERIFY_PROGRAM = (
    _COMMON_PROGRAM
    + r"""
account_file = Path(sys.argv[2]).resolve()
expected_state = sys.argv[3]
expected_order = sys.argv[4]
expected_count = int(sys.argv[5])
expected_raw = sys.argv[6]
expected_size = int(sys.argv[7])
expected_device = int(sys.argv[8])
expected_inode = int(sys.argv[9])

before_identity, before_raw = raw_snapshot(account_file, expected_size)
store = released_store.AccountStore(account_file).load()
accounts = list(store)
state = {account.label: account.to_dict() for account in accounts}
order = [[account.label, account.provider_id] for account in accounts]
after_identity, after_raw = raw_snapshot(account_file, expected_size)
if (
    len(accounts) != expected_count
    or before_identity[:2] != (expected_device, expected_inode)
    or before_identity != after_identity
    or before_raw != expected_raw
    or after_raw != expected_raw
    or digest(state) != expected_state
    or digest(order) != expected_order
):
    raise SystemExit(11)
sys.stdout.write("sidekick-v060-reader-verified\n")
"""
)


class RollbackOracleUnavailableError(PersistenceError):
    """The pinned reader cannot be proven before rollback mutation."""

    def __init__(self) -> None:
        self.code = PersistenceCode.ROLLBACK_REQUIRED
        super().__init__(
            "The bundled v0.6.0 rollback verifier is unavailable; "
            "account state was not changed."
        )


class ReleasedReaderVerificationError(PersistenceError):
    """The committed compatibility state failed the released reader."""

    def __init__(self) -> None:
        self.code = PersistenceCode.DURABILITY_UNCERTAIN
        super().__init__(
            "The released v0.6.0 reader could not verify rollback state."
        )


type ProcessRunner = Callable[
    [tuple[str, ...], Path, Mapping[str, str]],
    subprocess.CompletedProcess[str],
]


def run_process(
    argv: tuple[str, ...],
    cwd: Path,
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run one bounded isolated compatibility subprocess."""
    return subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        capture_output=True,
        check=False,
        encoding="utf-8",
        errors="strict",
        timeout=_COMMAND_TIMEOUT_SECONDS,
    )


@dataclass(frozen=True, slots=True)
class ReleasedV060Verifier:
    """Verify rollback bytes with the bundled exact released reader."""

    runner: ProcessRunner = run_process

    def preflight(self) -> None:
        """Prove the pinned bundle can execute before any mutation."""
        with self._bundle_path(preflight=True) as bundle_path:
            result = self._run(
                (
                    sys.executable,
                    "-I",
                    "-X",
                    "utf8",
                    "-c",
                    _PREFLIGHT_PROGRAM,
                    str(bundle_path),
                ),
                preflight=True,
            )
        if (
            result.returncode != 0
            or result.stdout.strip() != _PREFLIGHT_SENTINEL
        ):
            raise RollbackOracleUnavailableError

    def verify(self, account_path: Path, expected: FileSnapshot) -> None:
        """Run the old reader against the exact committed authority.

        :param account_path: Absolute generation-zero authority path.
        :param expected: Reopened commit proof the old reader must observe.
        """
        if not account_path.is_absolute():
            raise ValueError("Rollback authority path must be absolute.")
        if expected.link_count != 1:
            raise ReleasedReaderVerificationError
        expected_payload = expected.data
        document = decode_generation_zero(expected_payload)
        expected_state = _state_digest(expected_payload)
        expected_order = _order_digest(document)
        with self._bundle_path(preflight=False) as bundle_path:
            result = self._run(
                (
                    sys.executable,
                    "-I",
                    "-X",
                    "utf8",
                    "-c",
                    _VERIFY_PROGRAM,
                    str(bundle_path),
                    str(account_path),
                    expected_state,
                    expected_order,
                    str(len(document.accounts)),
                    hashlib.sha256(expected_payload).hexdigest(),
                    str(len(expected_payload)),
                    str(expected.fingerprint.identity.device),
                    str(expected.fingerprint.identity.inode),
                ),
                preflight=False,
            )
        if result.returncode != 0 or result.stdout.strip() != _VERIFY_SENTINEL:
            raise ReleasedReaderVerificationError

    @contextmanager
    def _bundle_path(
        self,
        *,
        preflight: bool,
    ) -> Iterator[Path]:
        bundle = resources.files("sidekick_usages.persistence").joinpath(
            *_BUNDLE_RELATIVE_PATH
        )
        try:
            with resources.as_file(bundle) as bundle_path:
                payload = bundle_path.read_bytes()
                if (
                    hashlib.sha256(payload).hexdigest()
                    != V060_READER_BUNDLE_SHA256
                ):
                    if preflight:
                        raise RollbackOracleUnavailableError
                    raise ReleasedReaderVerificationError
                yield bundle_path
        except OSError:
            if preflight:
                raise RollbackOracleUnavailableError from None
            raise ReleasedReaderVerificationError from None

    def _run(
        self,
        argv: tuple[str, ...],
        *,
        preflight: bool,
    ) -> subprocess.CompletedProcess[str]:
        try:
            with tempfile.TemporaryDirectory(
                prefix="sidekick-v060-reader-"
            ) as temporary:
                sandbox = Path(temporary)
                return self.runner(
                    argv,
                    sandbox,
                    _isolated_environment(sandbox),
                )
        except OSError, subprocess.SubprocessError, UnicodeError:
            if preflight:
                raise RollbackOracleUnavailableError from None
            raise ReleasedReaderVerificationError from None


def _state_digest(payload: bytes) -> str:
    try:
        value = decode_json_value(payload)
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except TypeError, ValueError, UnicodeError:
        raise ReleasedReaderVerificationError from None
    return hashlib.sha256(canonical).hexdigest()


def _order_digest(document: GenerationZeroDocument) -> str:
    order = [
        [str(record.label), record.provider_id.value]
        for record in document.accounts
    ]
    payload = json.dumps(
        order,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _isolated_environment(sandbox: Path) -> dict[str, str]:
    environment = {
        "HOME": str(sandbox),
        "USERPROFILE": str(sandbox),
    }
    for name in ("SYSTEMROOT", "WINDIR"):
        if (value := os.environ.get(name)) is not None:
            environment[name] = value
    return environment


__all__ = [
    "PINNED_V060_COMMIT",
    "V060_READER_BUNDLE_SHA256",
    "ReleasedReaderVerificationError",
    "ReleasedV060Verifier",
    "RollbackOracleUnavailableError",
]

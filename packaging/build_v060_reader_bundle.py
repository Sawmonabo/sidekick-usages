#!/usr/bin/env python3
"""Build the deterministic bundled v0.6.0 account reader."""

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from sidekick_usages.persistence.v060 import PINNED_V060_COMMIT

_RELEASE_FILES = (
    "src/sidekick_usages/__init__.py",
    "src/sidekick_usages/errors.py",
    "src/sidekick_usages/store.py",
)
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_REGULAR_FILE_MODE = 0o100644


class BundleBuildError(RuntimeError):
    """The exact pinned reader bundle could not be produced."""


def _git_file(repository: Path, source_path: str) -> bytes:
    try:
        result = subprocess.run(
            (
                "git",
                "-C",
                str(repository),
                "show",
                f"{PINNED_V060_COMMIT}:{source_path}",
            ),
            check=False,
            capture_output=True,
            timeout=30,
        )
    except OSError, subprocess.SubprocessError:
        raise BundleBuildError(
            "The pinned release source is unavailable."
        ) from None
    if result.returncode != 0:
        raise BundleBuildError("The pinned release source is unavailable.")
    return result.stdout


def _zip_entry(name: str) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
    entry.compress_type = zipfile.ZIP_STORED
    entry.create_system = 3
    entry.external_attr = _REGULAR_FILE_MODE << 16
    return entry


def build_bundle(repository: Path, output: Path) -> str:
    """Write the exact deterministic release bundle and return its digest."""
    sources = {
        source_path.removeprefix("src/"): _git_file(
            repository,
            source_path,
        )
        for source_path in _RELEASE_FILES
    }
    manifest = {
        "commit": PINNED_V060_COMMIT,
        "files": {
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in sorted(sources.items())
        },
        "version": "0.6.0",
    }
    manifest_bytes = (
        json.dumps(
            manifest,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output,
        mode="w",
        compression=zipfile.ZIP_STORED,
    ) as bundle:
        for name, payload in sorted(sources.items()):
            bundle.writestr(_zip_entry(name), payload)
        bundle.writestr(_zip_entry("MANIFEST.json"), manifest_bytes)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> int:
    """Build the bundle from command-line paths."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository",
        type=Path,
        default=Path.cwd(),
        help="Git checkout containing the pinned commit.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination ZIP path.",
    )
    arguments = parser.parse_args()
    try:
        digest = build_bundle(
            arguments.repository.resolve(),
            arguments.output.resolve(),
        )
    except BundleBuildError as error:
        parser.exit(1, f"error: {error}\n")
    sys.stdout.write(f"{digest}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

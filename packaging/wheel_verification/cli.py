"""Exact-wheel verifier command-line composition."""

import argparse
import sys
import tempfile
from pathlib import Path

from wheel_verification.errors import WheelVerificationError
from wheel_verification.project import load_project_contract
from wheel_verification.service import (
    build_distributions,
    verify_exact_wheel,
)

PYPROJECT_FILENAME = "pyproject.toml"


def _parser() -> argparse.ArgumentParser:
    """Return the release-verification argument parser."""
    parser = argparse.ArgumentParser(
        description="Build or verify the exact sidekick-usages wheel.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--build",
        action="store_true",
        help="Build into a fresh directory before verification.",
    )
    mode.add_argument(
        "--wheel",
        type=Path,
        help="Verify this exact pre-built wheel.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Fresh artifact directory retained after --build.",
    )
    return parser


def _write_success(benchmark_report: str, wheel: str) -> None:
    """Write installed-wheel measurements and the verified artifact."""
    sys.stdout.write(benchmark_report)
    if benchmark_report and not benchmark_report.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.write(f"Verified exact wheel: {wheel}\n")


def main(repository_root: Path) -> int:
    """Run the selected artifact verification mode."""
    args = _parser().parse_args()
    if args.output_dir is not None and not args.build:
        raise WheelVerificationError("--output-dir requires --build.")
    contract = load_project_contract(
        repository_root,
        repository_root / PYPROJECT_FILENAME,
    )

    if args.build and args.output_dir is None:
        with tempfile.TemporaryDirectory(
            prefix="sidekick-wheel-build-"
        ) as raw:
            wheel = build_distributions(contract, Path(raw) / "artifacts")
            benchmark_report = verify_exact_wheel(contract, wheel)
            _write_success(benchmark_report, wheel.name)
        return 0

    if args.build:
        wheel = build_distributions(contract, args.output_dir.resolve())
    elif args.wheel is not None:
        wheel = args.wheel.resolve()
    else:
        raise WheelVerificationError("One verification mode is required.")
    benchmark_report = verify_exact_wheel(contract, wheel)
    _write_success(benchmark_report, str(wheel))
    return 0

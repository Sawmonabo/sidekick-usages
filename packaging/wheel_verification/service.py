"""Exact-distribution build and verification orchestration."""

import os
from pathlib import Path

from wheel_verification import artifacts, project, runtime
from wheel_verification.errors import WheelVerificationError
from wheel_verification.models import ProjectContract


def build_distributions(
    contract: ProjectContract,
    output_dir: Path,
) -> Path:
    """Build into a new output directory and return the exact wheel."""
    if output_dir.exists():
        raise WheelVerificationError(
            f"Refusing non-fresh output directory: {output_dir}"
        )
    output_dir.mkdir(parents=True)
    runtime.run_command(
        [
            runtime.require_uv_executable(),
            "build",
            "--out-dir",
            str(output_dir),
            "--no-create-gitignore",
        ],
        cwd=contract.repository_root,
        env=os.environ.copy(),
    )
    wheel, _ = artifacts.require_exact_distribution_set(contract, output_dir)
    return wheel


def verify_exact_wheel(
    contract: ProjectContract,
    wheel: Path,
) -> str:
    """Verify one explicitly selected wheel and its isolated runtime."""
    selected, sdist = artifacts.require_exact_distribution_set(
        contract,
        wheel.parent,
    )
    if selected.resolve() != wheel.resolve():
        raise WheelVerificationError(
            f"Selected wheel is not the exact artifact: {wheel}"
        )
    project.verify_source_members(contract)
    artifacts.verify_sdist_members(contract, sdist)
    artifacts.verify_wheel_members(contract, selected)
    return runtime.verify_installed_wheel(contract, selected)

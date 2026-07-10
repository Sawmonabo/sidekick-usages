#!/usr/bin/env python3
"""Build and verify the exact sidekick-usages wheel in isolation."""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"

REQUIRED_WHEEL_MEMBERS = frozenset(
    {
        "sidekick_usages/__init__.py",
        "sidekick_usages/__main__.py",
        "sidekick_usages/cli.py",
        "sidekick_usages/http/__init__.py",
        "sidekick_usages/http/client.py",
        "sidekick_usages/http/retry.py",
        "sidekick_usages/serialization/__init__.py",
        "sidekick_usages/serialization/json.py",
    }
)
FORBIDDEN_WHEEL_MEMBERS = frozenset({"sidekick_usages/http.py"})


class WheelVerificationError(RuntimeError):
    """The built artifact does not satisfy the release contract."""


def project_identity() -> tuple[str, str]:
    """Return the normalized distribution name and declared version.

    :return: ``(wheel_distribution_name, version)``.
    """
    project = tomllib.loads(PYPROJECT_PATH.read_text())["project"]
    name = str(project["name"]).replace("-", "_").replace(".", "_")
    return name, str(project["version"])


def expected_artifact_names() -> tuple[str, str]:
    """Return the exact wheel and source-distribution filenames.

    :return: ``(wheel_filename, sdist_filename)``.
    """
    name, version = project_identity()
    return (
        f"{name}-{version}-py3-none-any.whl",
        f"{name}-{version}.tar.gz",
    )


def require_exact_wheel(directory: Path) -> Path:
    """Select the sole wheel only when its filename is exact.

    :param directory: Artifact directory to inspect.
    :return: The exact wheel path.
    :raises WheelVerificationError: If the directory is missing or its wheel
        set is not exactly the expected singleton.
    """
    expected_wheel, _ = expected_artifact_names()
    if not directory.is_dir():
        raise WheelVerificationError(
            f"Artifact directory does not exist: {directory}"
        )
    wheel_names = sorted(
        path.name for path in directory.iterdir() if path.suffix == ".whl"
    )
    if wheel_names != [expected_wheel]:
        raise WheelVerificationError(
            "Expected exactly "
            f"{expected_wheel!r}; found wheels {wheel_names!r}."
        )
    return directory / expected_wheel


def require_exact_sdist(directory: Path) -> Path:
    """Select the sole source distribution when its filename is exact.

    :param directory: Artifact directory to inspect.
    :return: The exact source-distribution path.
    :raises WheelVerificationError: If the sdist set is not exact.
    """
    _, expected_sdist = expected_artifact_names()
    sdist_names = sorted(
        path.name
        for path in directory.iterdir()
        if path.name.endswith(".tar.gz")
    )
    if sdist_names != [expected_sdist]:
        raise WheelVerificationError(
            "Expected exactly "
            f"{expected_sdist!r}; found sdists {sdist_names!r}."
        )
    return directory / expected_sdist


def require_exact_distribution_set(directory: Path) -> tuple[Path, Path]:
    """Require one exact wheel, one exact sdist, and no sibling files.

    :param directory: Downloaded or newly built distribution directory.
    :return: ``(wheel, sdist)``.
    :raises WheelVerificationError: If an unexpected sibling artifact exists.
    """
    wheel = require_exact_wheel(directory)
    sdist = require_exact_sdist(directory)
    expected = sorted((wheel.name, sdist.name))
    found = sorted(path.name for path in directory.iterdir() if path.is_file())
    if found != expected:
        raise WheelVerificationError(
            f"Expected distribution set {expected!r}; found {found!r}."
        )
    return wheel, sdist


def verify_wheel_members(wheel: Path) -> None:
    """Verify required package members and reject stale module remnants.

    :param wheel: Exact wheel to inspect.
    :raises WheelVerificationError: If the archive is malformed or its member
        contract is violated.
    """
    try:
        with zipfile.ZipFile(wheel) as archive:
            members = frozenset(archive.namelist())
    except zipfile.BadZipFile as error:
        raise WheelVerificationError(
            f"Invalid wheel archive: {wheel}"
        ) from error

    missing = sorted(REQUIRED_WHEEL_MEMBERS - members)
    forbidden = sorted(FORBIDDEN_WHEEL_MEMBERS & members)
    if missing or forbidden:
        raise WheelVerificationError(
            f"Wheel member contract failed; missing={missing!r}, "
            f"forbidden={forbidden!r}."
        )


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    """Run a UTF-8 verifier subprocess with useful failure diagnostics."""
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            encoding="utf-8",
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "no output").strip()
        raise WheelVerificationError(
            f"Command failed ({error.returncode}): {command!r}\n{detail}"
        ) from error


def _uv_executable() -> str:
    """Return the uv executable or fail with an actionable message."""
    if (uv := shutil.which("uv")) is None:
        raise WheelVerificationError("`uv` is required to verify the wheel.")
    return uv


def _venv_commands(venv: Path) -> tuple[Path, Path]:
    """Return the platform-specific Python and console entry points."""
    if os.name == "nt":
        scripts = venv / "Scripts"
        return scripts / "python.exe", scripts / "sidekick-usages.exe"
    scripts = venv / "bin"
    return scripts / "python", scripts / "sidekick-usages"


def _clean_subprocess_env() -> dict[str, str]:
    """Return an environment without source or active-venv leakage."""
    env = os.environ.copy()
    for name in (
        "CONDA_PREFIX",
        "PYTHONHOME",
        "PYTHONPATH",
        "UV_PROJECT_ENVIRONMENT",
        "VIRTUAL_ENV",
    ):
        env.pop(name, None)
    env.update(
        {
            "COLUMNS": "120",
            "NO_COLOR": "1",
            # Captured standard streams are pipes, where Python honors this
            # override on Windows as well as POSIX hosts.
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "TERM": "dumb",
        }
    )
    return env


def _isolated_command_env(home: Path) -> dict[str, str]:
    """Return a clean command environment rooted at an absent home."""
    env = _clean_subprocess_env()
    env.update({"HOME": str(home), "USERPROFILE": str(home)})
    if os.name == "nt":
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    return env


def verify_installed_wheel(wheel: Path) -> None:
    """Install the wheel into a fresh venv and exercise both entry paths.

    :param wheel: Exact wheel to install.
    :raises WheelVerificationError: If installation, import isolation, or a
        command smoke check fails.
    """
    uv = _uv_executable()
    with tempfile.TemporaryDirectory(prefix="sidekick-wheel-runtime-") as raw:
        runtime_root = Path(raw)
        venv = runtime_root / "venv"
        run_dir = runtime_root / "outside-source"
        run_dir.mkdir()
        install_env = _clean_subprocess_env()
        env = _isolated_command_env(runtime_root / "absent-home")

        _run(
            [
                uv,
                "venv",
                "--no-project",
                "--python",
                sys.executable,
                str(venv),
            ],
            cwd=run_dir,
            env=install_env,
        )
        python, console = _venv_commands(venv)
        _run(
            [uv, "pip", "install", "--python", str(python), str(wheel)],
            cwd=run_dir,
            env=install_env,
        )

        origin_check = (
            "import pathlib, sidekick_usages, sys; "
            "origin = pathlib.Path(sidekick_usages.__file__).resolve(); "
            "prefix = pathlib.Path(sys.prefix).resolve(); "
            "assert origin.is_relative_to(prefix), (origin, prefix)"
        )
        _run(
            [str(python), "-c", origin_check],
            cwd=run_dir,
            env=env,
        )

        smoke_arguments = (
            ("--version",),
            ("--help",),
            ("daemon", "status", "--help"),
            ("add", "--help"),
        )
        entry_points = (
            (str(console),),
            (str(python), "-m", "sidekick_usages"),
        )
        for entry_point in entry_points:
            for arguments in smoke_arguments:
                result = _run(
                    [*entry_point, *arguments],
                    cwd=run_dir,
                    env=env,
                )
                if arguments == ("--help",) and (
                    "┴" not in result.stdout
                    or "sidekick usages" not in result.stdout
                ):
                    raise WheelVerificationError(
                        "Root help omitted the Unicode robot header."
                    )

        if Path(env["HOME"]).exists():
            raise WheelVerificationError(
                "Help or version created files below the isolated home."
            )


def build_distributions(output_dir: Path) -> Path:
    """Build into a new output directory and return the exact wheel.

    :param output_dir: Directory that must not exist before this call.
    :return: Exact newly built wheel.
    :raises WheelVerificationError: If the directory already exists or the
        built artifact set is not exact.
    """
    if output_dir.exists():
        raise WheelVerificationError(
            f"Refusing non-fresh output directory: {output_dir}"
        )
    output_dir.mkdir(parents=True)
    _run(
        [
            _uv_executable(),
            "build",
            "--out-dir",
            str(output_dir),
            "--no-create-gitignore",
        ],
        cwd=REPO_ROOT,
        env=os.environ.copy(),
    )
    wheel, _ = require_exact_distribution_set(output_dir)
    return wheel


def verify_exact_wheel(wheel: Path) -> None:
    """Verify one explicitly selected wheel and its isolated runtime.

    :param wheel: Wheel path whose name and sibling wheel set must be exact.
    :raises WheelVerificationError: If any artifact or runtime gate fails.
    """
    selected, _ = require_exact_distribution_set(wheel.parent)
    if selected.resolve() != wheel.resolve():
        raise WheelVerificationError(
            f"Selected wheel is not the exact artifact: {wheel}"
        )
    verify_wheel_members(selected)
    verify_installed_wheel(selected)


def _parser() -> argparse.ArgumentParser:
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


def main() -> int:
    """Run the selected artifact verification mode."""
    args = _parser().parse_args()
    if args.output_dir is not None and not args.build:
        raise WheelVerificationError("--output-dir requires --build.")

    if args.build and args.output_dir is None:
        with tempfile.TemporaryDirectory(
            prefix="sidekick-wheel-build-"
        ) as raw:
            wheel = build_distributions(Path(raw) / "artifacts")
            verify_exact_wheel(wheel)
            sys.stdout.write(f"Verified exact wheel: {wheel.name}\n")
        return 0

    if args.build:
        wheel = build_distributions(args.output_dir.resolve())
    else:
        wheel = args.wheel.resolve()
    verify_exact_wheel(wheel)
    sys.stdout.write(f"Verified exact wheel: {wheel}\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except WheelVerificationError as error:
        sys.stderr.write(f"wheel verification failed: {error}\n")
        sys.exit(1)

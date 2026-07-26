"""Isolated build and installed-wheel runtime verification."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from wheel_verification import project
from wheel_verification.errors import WheelVerificationError
from wheel_verification.models import ProjectContract

ENVIRONMENT_VARIABLES_TO_CLEAR = (
    "CONDA_PREFIX",
    "PYTHONHOME",
    "PYTHONPATH",
    "UV_PROJECT_ENVIRONMENT",
    "VIRTUAL_ENV",
)
SMOKE_ARGUMENTS: tuple[tuple[str, ...], ...] = (
    ("--version",),
    ("--help",),
    ("daemon", "--help"),
    ("daemon", "status", "--help"),
    ("doctor", "--help"),
    ("add", "--help"),
    ("claude", "--help"),
    ("claude", "setup-token", "--help"),
    ("codex", "--help"),
    ("codex", "login", "--help"),
)
INTERNAL_ENTRY_POINT_TARGETS = frozenset(
    {
        "sidekick_usages.entrypoints.supervisor:main",
        "sidekick_usages.entrypoints.worker:main",
    }
)
INSTALLED_ORIGIN_CHECK = """
import importlib.metadata
import pathlib
import sys

import platformdirs
import sidekick_usages

origin = pathlib.Path(sidekick_usages.__file__).resolve()
prefix = pathlib.Path(sys.prefix).resolve()
dependency = pathlib.Path(platformdirs.__file__).resolve()
assert origin.is_relative_to(prefix), (origin, prefix)
assert dependency.is_relative_to(prefix), (dependency, prefix)
assert importlib.metadata.version("platformdirs") == "4.10.0"
"""
ENTRY_POINT_INVENTORY_CHECK = """
import importlib.metadata
import sys

distribution = importlib.metadata.distribution("sidekick-usages")
points = {
    point.name: point.value
    for point in distribution.entry_points
    if point.group == "console_scripts"
}
expected = dict(argument.split("=", 1) for argument in sys.argv[1:])
assert points == expected, points
"""
INTERNAL_ENTRY_POINT_CHECK = """
import sidekick_usages.entrypoints.supervisor
import sidekick_usages.entrypoints.worker

assert callable(sidekick_usages.entrypoints.supervisor.main)
assert callable(sidekick_usages.entrypoints.worker.main)
"""


def run_command(
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


def require_uv_executable() -> str:
    """Return the uv executable or fail with an actionable message."""
    if (uv := shutil.which("uv")) is None:
        raise WheelVerificationError("`uv` is required to verify the wheel.")
    return uv


def _venv_python(venv: Path) -> Path:
    """Return the platform-specific virtual-environment Python."""
    if os.name == "nt":
        scripts = venv / "Scripts"
        return scripts / "python.exe"
    scripts = venv / "bin"
    return scripts / "python"


def _clean_subprocess_env() -> dict[str, str]:
    """Return an environment without source or active-venv leakage."""
    env = os.environ.copy()
    for name in ENVIRONMENT_VARIABLES_TO_CLEAR:
        env.pop(name, None)
    env.update(
        {
            "COLUMNS": "120",
            "NO_COLOR": "1",
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


def verify_installed_wheel(
    contract: ProjectContract,
    wheel: Path,
) -> None:
    """Install the wheel and exercise both entry paths outside the checkout."""
    uv = require_uv_executable()
    with tempfile.TemporaryDirectory(prefix="sidekick-wheel-runtime-") as raw:
        runtime_root = Path(raw)
        venv = runtime_root / "venv"
        run_dir = runtime_root / "outside-source"
        run_dir.mkdir()
        install_env = _clean_subprocess_env()
        env = _isolated_command_env(runtime_root / "absent-home")

        run_command(
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
        python = _venv_python(venv)
        scripts_root = python.parent
        scripts = {
            name: scripts_root / (f"{name}.exe" if os.name == "nt" else name)
            for name, _ in contract.scripts
        }
        run_command(
            [uv, "pip", "install", "--python", str(python), str(wheel)],
            cwd=run_dir,
            env=install_env,
        )
        missing_scripts = [
            name for name, script in scripts.items() if not script.is_file()
        ]
        if missing_scripts:
            raise WheelVerificationError(
                f"Installed console scripts are missing: {missing_scripts!r}."
            )

        run_command(
            [
                str(python),
                "-c",
                ENTRY_POINT_INVENTORY_CHECK,
                *(f"{name}={target}" for name, target in contract.scripts),
            ],
            cwd=run_dir,
            env=env,
        )
        internal_targets = frozenset(
            target
            for _, target in contract.scripts
            if target != project.PUBLIC_CLI_TARGET
        )
        if internal_targets != INTERNAL_ENTRY_POINT_TARGETS:
            raise WheelVerificationError(
                "Internal console-script targets do not match the runtime "
                f"contract: {sorted(internal_targets)!r}."
            )
        run_command(
            [str(python), "-c", INTERNAL_ENTRY_POINT_CHECK],
            cwd=run_dir,
            env=env,
        )
        run_command(
            [str(python), "-c", INSTALLED_ORIGIN_CHECK],
            cwd=run_dir,
            env=env,
        )

        public_script = scripts[project.public_cli_script(contract)]
        entry_points = (
            (str(public_script),),
            (str(python), "-m", "sidekick_usages"),
        )
        for entry_point in entry_points:
            for arguments in SMOKE_ARGUMENTS:
                result = run_command(
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

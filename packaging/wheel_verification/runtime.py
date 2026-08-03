"""Isolated build and installed-wheel runtime verification."""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dashboard_benchmark.command import DASHBOARD_BENCHMARK_SUCCESS
from dashboard_benchmark.environment import isolated_console_environment
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
DASHBOARD_BENCHMARK_RELATIVE_PATH = (
    Path("packaging") / "benchmark_dashboard.py"
)
DASHBOARD_BENCHMARK_HOME_PREFIX = "sidekick-dashboard-benchmark-home-"
PUBLIC_ROOT_USAGE = "Usage: sidekick-usages"
PRIVATE_ROOT_USAGE = "sidekick_usages.cli.runtime.application"
CLAUDE_SESSION_DISABLED = "claude session integration is not available"
CLAUDE_SESSION_NOT_STARTED = "provider process was not started"
SYNTHETIC_PROVIDER_SECTIONS = (
    "CLAUDE · 4 accounts",
    "CODEX · 2 accounts",
)
SHELL_DRY_RUN = "Dry run: would change."
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
import prompt_toolkit
import sidekick_usages
import wcwidth

origin = pathlib.Path(sidekick_usages.__file__).resolve()
prefix = pathlib.Path(sys.prefix).resolve()
dependencies = (
    pathlib.Path(platformdirs.__file__).resolve(),
    pathlib.Path(prompt_toolkit.__file__).resolve(),
    pathlib.Path(wcwidth.__file__).resolve(),
)
assert origin.is_relative_to(prefix), (origin, prefix)
assert all(path.is_relative_to(prefix) for path in dependencies), dependencies
assert importlib.metadata.version("platformdirs") == "4.10.0"
assert importlib.metadata.version("prompt-toolkit") == "3.0.52"
assert importlib.metadata.version("wcwidth") == "0.7.0"
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
import sidekick_usages.entrypoints.dashboard
import sidekick_usages.entrypoints.supervisor
import sidekick_usages.entrypoints.usage_lookup
import sidekick_usages.entrypoints.worker

assert callable(sidekick_usages.entrypoints.dashboard.main)
assert callable(sidekick_usages.entrypoints.supervisor.main)
assert callable(sidekick_usages.entrypoints.usage_lookup.main)
assert callable(sidekick_usages.entrypoints.worker.main)
"""


def run_command(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    expected_exit_code: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Run a UTF-8 verifier subprocess with useful failure diagnostics."""
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        encoding="utf-8",
    )
    if result.returncode != expected_exit_code:
        detail = (result.stderr or result.stdout or "no output").strip()
        raise WheelVerificationError(
            f"Command failed ({result.returncode}): {command!r}\n{detail}"
        )
    return result


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


def _dashboard_benchmark_env(
    home: Path,
    public_script: Path,
) -> dict[str, str]:
    """Return a provider-free environment for the installed runtime gate."""
    return isolated_console_environment(
        _isolated_command_env(home),
        home=home,
        console_script=public_script,
    )


def _verify_dashboard_benchmark(
    contract: ProjectContract,
    python: Path,
    public_script: Path,
    run_dir: Path,
) -> str:
    """Run the release benchmark with the isolated wheel interpreter."""
    benchmark = contract.repository_root / DASHBOARD_BENCHMARK_RELATIVE_PATH
    if not benchmark.is_file():
        raise WheelVerificationError(
            f"Dashboard benchmark is missing: {benchmark}"
        )
    try:
        verified_public_script = public_script.resolve(strict=True)
    except OSError:
        raise WheelVerificationError(
            "Installed public console script is unavailable."
        ) from None
    with tempfile.TemporaryDirectory(
        prefix=DASHBOARD_BENCHMARK_HOME_PREFIX
    ) as raw_home:
        home = Path(raw_home).resolve()
        command = [str(python), str(benchmark)]
        env = _dashboard_benchmark_env(home, verified_public_script)
        if os.name != "nt":
            command.append(str(verified_public_script))
        result = run_command(
            command,
            cwd=run_dir,
            env=env,
        )
        report = run_command(
            [str(public_script), "--no-interactive", "check"],
            cwd=run_dir,
            env=env,
            expected_exit_code=1,
        )
        rendered = report.stdout + report.stderr
        if (
            any(
                section not in rendered
                for section in SYNTHETIC_PROVIDER_SECTIONS
            )
            or "External " in rendered
        ):
            raise WheelVerificationError(
                "Installed-wheel one-shot reporting violated its "
                "synthetic saved-account contract."
            )
        if os.name != "nt":
            shell = run_command(
                [
                    str(public_script),
                    "session",
                    "shell",
                    "install",
                    "--shell",
                    "bash",
                    "--dry-run",
                ],
                cwd=run_dir,
                env=env,
            )
            shell_integration = (
                Path(env["XDG_DATA_HOME"])
                / "sidekick-usages"
                / "shell-integration.sh"
            )
            if (
                SHELL_DRY_RUN not in shell.stdout
                or (home / ".bashrc").exists()
                or shell_integration.exists()
            ):
                raise WheelVerificationError(
                    "Installed-wheel shell dry-run changed isolated state."
                )
        claude = run_command(
            [
                str(public_script),
                "session",
                "claude",
                "--",
                "synthetic",
            ],
            cwd=run_dir,
            env=env,
            expected_exit_code=1,
        )
        claude_output = claude.stdout + claude.stderr
        if (
            CLAUDE_SESSION_DISABLED not in claude_output
            or CLAUDE_SESSION_NOT_STARTED not in claude_output
        ):
            raise WheelVerificationError(
                "Installed-wheel Claude session did not fail closed."
            )
    if DASHBOARD_BENCHMARK_SUCCESS not in result.stdout:
        raise WheelVerificationError(
            "Installed-wheel dashboard benchmark omitted its success proof."
        )
    return result.stdout


def verify_installed_wheel(
    contract: ProjectContract,
    wheel: Path,
) -> str:
    """Install the wheel and run its smoke and release gates in isolation."""
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
                    or PUBLIC_ROOT_USAGE not in result.stdout
                    or PRIVATE_ROOT_USAGE in result.stdout
                ):
                    raise WheelVerificationError(
                        "Root help violated the public console contract."
                    )

        if Path(env["HOME"]).exists():
            raise WheelVerificationError(
                "Installed-wheel smoke commands created files below the "
                "absent home."
            )
        return _verify_dashboard_benchmark(
            contract,
            python,
            public_script,
            run_dir,
        )

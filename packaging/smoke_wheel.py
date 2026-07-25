#!/usr/bin/env python3
"""Build and verify the exact sidekick-usages wheel in isolation."""

import argparse
import csv
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
MINIMUM_SOURCE_PACKAGE_PARTS = 2
RECORD_COLUMN_COUNT = 3
IGNORED_SOURCE_DIRECTORIES = frozenset({"__pycache__"})
UNSUPPORTED_SELECTION_KEYS = frozenset(
    {
        "artifacts",
        "exclude",
        "force-include",
        "include",
        "only-include",
        "sources",
    }
)
PUBLIC_CLI_TARGET = "sidekick_usages.cli.app:run"
VENDOR_COMMAND_NAMES = frozenset({"claude", "codex"})
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


class WheelVerificationError(RuntimeError):
    """The built artifact does not satisfy the release contract."""


def _project_configuration() -> dict[str, object]:
    """Return the strict project configuration mapping."""
    try:
        configuration = tomllib.loads(PYPROJECT_PATH.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise WheelVerificationError(
            f"Cannot read project configuration: {PYPROJECT_PATH}"
        ) from error
    return configuration


def _required_mapping(
    owner: dict[str, object],
    name: str,
) -> dict[str, object]:
    """Return one required nested project table."""
    value = owner.get(name)
    if not isinstance(value, dict) or any(
        not isinstance(key, str) for key in value
    ):
        raise WheelVerificationError(
            f"Project configuration table {name!r} is missing."
        )
    return {key: child for key, child in value.items() if isinstance(key, str)}


def project_identity() -> tuple[str, str]:
    """Return the normalized distribution name and declared version."""
    project = _required_mapping(_project_configuration(), "project")
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise WheelVerificationError(
            "Project name and version must be static strings."
        )
    return name.replace("-", "_").replace(".", "_"), version


def project_scripts() -> dict[str, str]:
    """Return the exact declared console-script target mapping."""
    project = _required_mapping(_project_configuration(), "project")
    scripts = _required_mapping(project, "scripts")
    if not scripts or any(
        not isinstance(name, str)
        or not name
        or not isinstance(target, str)
        or not target
        for name, target in scripts.items()
    ):
        raise WheelVerificationError(
            "Project console scripts must be nonempty string mappings."
        )
    vendor_conflicts = sorted(
        name for name in scripts if name.casefold() in VENDOR_COMMAND_NAMES
    )
    if vendor_conflicts:
        raise WheelVerificationError(
            f"Sidekick cannot replace provider commands: {vendor_conflicts!r}."
        )
    return {
        name: target
        for name, target in sorted(scripts.items())
        if isinstance(target, str)
    }


def public_cli_script() -> str:
    """Return the sole script declared for the public CLI target."""
    matches = [
        name
        for name, target in project_scripts().items()
        if target == PUBLIC_CLI_TARGET
    ]
    if len(matches) != 1:
        raise WheelVerificationError(
            "The public CLI target must have exactly one console script."
        )
    return matches[0]


def package_source_root() -> Path:
    """Return the sole package root declared by the Hatch wheel target."""
    tool = _required_mapping(_project_configuration(), "tool")
    hatch = _required_mapping(tool, "hatch")
    build = _required_mapping(hatch, "build")
    targets = _required_mapping(build, "targets")
    wheel = _required_mapping(targets, "wheel")
    unsupported = sorted(
        UNSUPPORTED_SELECTION_KEYS.intersection(build)
        | UNSUPPORTED_SELECTION_KEYS.intersection(wheel)
    )
    packages = wheel.get("packages")
    if unsupported:
        raise WheelVerificationError(
            "Wheel source derivation does not support additional selection "
            f"keys: {unsupported!r}."
        )
    if (
        not isinstance(packages, list)
        or len(packages) != 1
        or not isinstance(packages[0], str)
    ):
        raise WheelVerificationError(
            "The wheel target must declare exactly one package root."
        )
    relative = Path(packages[0])
    if (
        relative.is_absolute()
        or ".." in relative.parts
        or len(relative.parts) < MINIMUM_SOURCE_PACKAGE_PARTS
    ):
        raise WheelVerificationError(
            "The wheel package root must be a confined src-layout path."
        )
    root = REPO_ROOT / relative
    if not root.is_dir() or not (root / "__init__.py").is_file():
        raise WheelVerificationError(
            f"Declared wheel package root is invalid: {relative}"
        )
    return root


def expected_package_members() -> frozenset[str]:
    """Derive the exact pure-Python artifact contract from the package root."""
    root = package_source_root()
    package_name = root.name
    source_files: list[Path] = []
    unsupported: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if IGNORED_SOURCE_DIRECTORIES.intersection(relative.parts):
            continue
        if path.is_symlink() or path.suffix != ".py":
            unsupported.append(relative.as_posix())
            continue
        source_files.append(relative)
    if unsupported:
        raise WheelVerificationError(
            "The package contains undeclared data or unsafe links: "
            f"{sorted(unsupported)!r}."
        )
    members = frozenset(
        f"{package_name}/{relative.as_posix()}" for relative in source_files
    )
    if f"{package_name}/__init__.py" not in members:
        raise WheelVerificationError("The declared package is empty.")
    return members


def expected_artifact_names() -> tuple[str, str]:
    """Return the exact wheel and source-distribution filenames."""
    name, version = project_identity()
    return (
        f"{name}-{version}-py3-none-any.whl",
        f"{name}-{version}.tar.gz",
    )


def require_exact_wheel(directory: Path) -> Path:
    """Select the sole wheel only when its filename is exact."""
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
    """Select the sole source distribution when its filename is exact."""
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
    """Require one exact wheel, one exact sdist, and no sibling files."""
    wheel = require_exact_wheel(directory)
    sdist = require_exact_sdist(directory)
    expected = sorted((wheel.name, sdist.name))
    found = sorted(path.name for path in directory.iterdir() if path.is_file())
    if found != expected:
        raise WheelVerificationError(
            f"Expected distribution set {expected!r}; found {found!r}."
        )
    return wheel, sdist


def _verify_members(
    artifact: str,
    expected: frozenset[str],
    observed: frozenset[str],
) -> None:
    """Require exact source-derived package membership."""
    missing = sorted(expected - observed)
    unexpected = sorted(observed - expected)
    if missing or unexpected:
        raise WheelVerificationError(
            f"{artifact} member contract failed; missing={missing!r}, "
            f"unexpected={unexpected!r}."
        )


def verify_wheel_members(wheel: Path) -> None:
    """Verify exact source-derived package members without a file manifest."""
    expected = expected_package_members()
    prefix = f"{package_source_root().name}/"
    name, version = project_identity()
    record_name = f"{name}-{version}.dist-info/RECORD"
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = tuple(
                entry.filename
                for entry in archive.infolist()
                if not entry.is_dir()
            )
            record_rows = tuple(
                csv.reader(
                    archive.read(record_name).decode("utf-8").splitlines()
                )
            )
    except (OSError, zipfile.BadZipFile) as error:
        raise WheelVerificationError(
            f"Invalid wheel archive: {wheel}"
        ) from error
    except (KeyError, UnicodeDecodeError, csv.Error) as error:
        raise WheelVerificationError("Wheel RECORD is invalid.") from error
    if len(names) != len(set(names)):
        raise WheelVerificationError("Wheel contains duplicate members.")
    if any(len(row) != RECORD_COLUMN_COUNT for row in record_rows):
        raise WheelVerificationError("Wheel RECORD rows are invalid.")
    recorded = tuple(row[0] for row in record_rows)
    if len(recorded) != len(set(recorded)) or set(recorded) != set(names):
        raise WheelVerificationError(
            "Wheel members and RECORD inventory differ."
        )
    observed = frozenset(
        member for member in names if member.startswith(prefix)
    )
    recorded_package = frozenset(
        member for member in recorded if member.startswith(prefix)
    )
    _verify_members("Wheel", expected, observed)
    _verify_members("Wheel RECORD", expected, recorded_package)


def verify_source_members() -> None:
    """Verify the declared package remains pure Python and confined."""
    expected_package_members()


def verify_sdist_members(sdist: Path) -> None:
    """Verify the sdist contains the exact source-derived package tree."""
    expected_sdist = expected_artifact_names()[1]
    archive_root = expected_sdist.removesuffix(".tar.gz")
    package_root = package_source_root()
    source_parent = package_root.parent.relative_to(REPO_ROOT).as_posix()
    prefix = f"{archive_root}/{source_parent}/"
    expected = frozenset(
        prefix + member for member in expected_package_members()
    )
    package_prefix = prefix + f"{package_root.name}/"
    try:
        with tarfile.open(sdist, mode="r:gz") as archive:
            names = tuple(
                member.name
                for member in archive.getmembers()
                if member.isfile()
            )
    except (OSError, tarfile.TarError) as error:
        raise WheelVerificationError(
            f"Invalid source distribution archive: {sdist}"
        ) from error
    if len(names) != len(set(names)):
        raise WheelVerificationError(
            "Source distribution contains duplicate members."
        )
    observed = frozenset(
        member for member in names if member.startswith(package_prefix)
    )
    _verify_members("Source distribution", expected, observed)


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


def _venv_commands(venv: Path) -> tuple[Path, dict[str, Path]]:
    """Return the platform-specific Python and installed console scripts."""
    script_names = project_scripts()
    if os.name == "nt":
        scripts = venv / "Scripts"
        return (
            scripts / "python.exe",
            {name: scripts / f"{name}.exe" for name in script_names},
        )
    scripts = venv / "bin"
    return (
        scripts / "python",
        {name: scripts / name for name in script_names},
    )


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


def _installed_origin_check() -> str:
    """Return the isolated installed-module provenance check."""
    return """
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


def _entry_point_inventory_check() -> str:
    """Return the exact installed entry-point mapping check."""
    return """
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


def _entry_point_load_check() -> str:
    """Return one fresh-process callable entry-point check."""
    return """
import importlib.metadata
import sys

name, target = sys.argv[1:]
distribution = importlib.metadata.distribution("sidekick-usages")
matches = [
    point
    for point in distribution.entry_points
    if point.group == "console_scripts" and point.name == name
]
assert len(matches) == 1, matches
assert matches[0].value == target, matches[0].value
assert callable(matches[0].load())
"""


def verify_installed_wheel(wheel: Path) -> None:
    """Install the wheel and exercise both entry paths outside the checkout."""
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
        python, scripts = _venv_commands(venv)
        _run(
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

        declared_scripts = project_scripts()
        _run(
            [
                str(python),
                "-c",
                _entry_point_inventory_check(),
                *(
                    f"{name}={target}"
                    for name, target in declared_scripts.items()
                ),
            ],
            cwd=run_dir,
            env=env,
        )
        for name, target in declared_scripts.items():
            _run(
                [
                    str(python),
                    "-c",
                    _entry_point_load_check(),
                    name,
                    target,
                ],
                cwd=run_dir,
                env=env,
            )
        _run(
            [str(python), "-c", _installed_origin_check()],
            cwd=run_dir,
            env=env,
        )

        public_script = scripts[public_cli_script()]
        entry_points = (
            (str(public_script),),
            (str(python), "-m", "sidekick_usages"),
        )
        for entry_point in entry_points:
            for arguments in SMOKE_ARGUMENTS:
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
    """Build into a new output directory and return the exact wheel."""
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
    """Verify one explicitly selected wheel and its isolated runtime."""
    selected, sdist = require_exact_distribution_set(wheel.parent)
    if selected.resolve() != wheel.resolve():
        raise WheelVerificationError(
            f"Selected wheel is not the exact artifact: {wheel}"
        )
    verify_source_members()
    verify_sdist_members(sdist)
    verify_wheel_members(selected)
    verify_installed_wheel(selected)


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

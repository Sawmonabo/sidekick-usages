"""Project configuration and source-package contracts."""

import tomllib
from pathlib import Path

from wheel_verification.errors import WheelVerificationError
from wheel_verification.models import ProjectContract

MINIMUM_SOURCE_PACKAGE_PARTS = 2
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


def _project_configuration(path: Path) -> dict[str, object]:
    """Return the strict project configuration mapping."""
    try:
        configuration = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise WheelVerificationError(
            f"Cannot read project configuration: {path}"
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


def _project_identity(
    configuration: dict[str, object],
) -> tuple[str, str]:
    """Return the normalized distribution name and declared version."""
    project = _required_mapping(configuration, "project")
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        raise WheelVerificationError(
            "Project name and version must be static strings."
        )
    return name.replace("-", "_").replace(".", "_"), version


def _project_scripts(
    configuration: dict[str, object],
) -> tuple[tuple[str, str], ...]:
    """Return the exact declared console-script targets."""
    project = _required_mapping(configuration, "project")
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
    return tuple(
        (name, target)
        for name, target in sorted(scripts.items())
        if isinstance(target, str)
    )


def _package_source_root(
    configuration: dict[str, object],
    repository_root: Path,
) -> Path:
    """Return the sole package root declared by the Hatch wheel target."""
    tool = _required_mapping(configuration, "tool")
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
    root = repository_root / relative
    if not root.is_dir() or not (root / "__init__.py").is_file():
        raise WheelVerificationError(
            f"Declared wheel package root is invalid: {relative}"
        )
    return root


def load_project_contract(
    repository_root: Path,
    pyproject_path: Path,
) -> ProjectContract:
    """Load one validated project and wheel-source declaration."""
    configuration = _project_configuration(pyproject_path)
    distribution_name, version = _project_identity(configuration)
    return ProjectContract(
        repository_root=repository_root,
        package_root=_package_source_root(configuration, repository_root),
        distribution_name=distribution_name,
        version=version,
        scripts=_project_scripts(configuration),
    )


def expected_package_members(
    contract: ProjectContract,
) -> frozenset[str]:
    """Derive the exact pure-Python artifact contract from its package root."""
    package_name = contract.package_root.name
    source_files: list[Path] = []
    unsupported: list[str] = []
    for path in contract.package_root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(contract.package_root)
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


def public_cli_script(contract: ProjectContract) -> str:
    """Return the sole script declared for the public CLI target."""
    matches = [
        name
        for name, target in contract.scripts
        if target == PUBLIC_CLI_TARGET
    ]
    if len(matches) != 1:
        raise WheelVerificationError(
            "The public CLI target must have exactly one console script."
        )
    return matches[0]


def verify_source_members(contract: ProjectContract) -> None:
    """Verify the declared package remains pure Python and confined."""
    expected_package_members(contract)

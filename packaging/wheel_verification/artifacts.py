"""Exact wheel and source-distribution inspection."""

import csv
import tarfile
import zipfile
from pathlib import Path

from wheel_verification import project
from wheel_verification.errors import WheelVerificationError
from wheel_verification.models import ProjectContract

RECORD_COLUMN_COUNT = 3


def expected_artifact_names(
    contract: ProjectContract,
) -> tuple[str, str]:
    """Return the exact wheel and source-distribution filenames."""
    return (
        f"{contract.distribution_name}-{contract.version}-py3-none-any.whl",
        f"{contract.distribution_name}-{contract.version}.tar.gz",
    )


def require_exact_wheel(
    contract: ProjectContract,
    directory: Path,
) -> Path:
    """Select the sole wheel only when its filename is exact."""
    expected_wheel, _ = expected_artifact_names(contract)
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


def require_exact_sdist(
    contract: ProjectContract,
    directory: Path,
) -> Path:
    """Select the sole source distribution when its filename is exact."""
    _, expected_sdist = expected_artifact_names(contract)
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


def require_exact_distribution_set(
    contract: ProjectContract,
    directory: Path,
) -> tuple[Path, Path]:
    """Require one exact wheel, one exact sdist, and no sibling files."""
    wheel = require_exact_wheel(contract, directory)
    sdist = require_exact_sdist(contract, directory)
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


def verify_wheel_members(
    contract: ProjectContract,
    wheel: Path,
) -> None:
    """Verify exact source-derived members and their RECORD inventory."""
    expected = project.expected_package_members(contract)
    prefix = f"{contract.package_root.name}/"
    record_name = (
        f"{contract.distribution_name}-{contract.version}.dist-info/RECORD"
    )
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


def verify_sdist_members(
    contract: ProjectContract,
    sdist: Path,
) -> None:
    """Verify the sdist contains the exact source-derived package tree."""
    expected_sdist = expected_artifact_names(contract)[1]
    archive_root = expected_sdist.removesuffix(".tar.gz")
    source_parent = contract.package_root.parent.relative_to(
        contract.repository_root
    ).as_posix()
    prefix = f"{archive_root}/{source_parent}/"
    expected = frozenset(
        prefix + member
        for member in project.expected_package_members(contract)
    )
    package_prefix = prefix + f"{contract.package_root.name}/"
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

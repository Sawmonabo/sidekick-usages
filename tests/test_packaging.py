"""Release artifact and dependency packaging contracts."""

import io
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from wheel_verification import artifacts, project
from wheel_verification.errors import WheelVerificationError

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_project(root: Path) -> Path:
    """Create one minimal pure-Python Hatch project."""
    package = root / "src" / "sample_package"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "feature.py").write_text("ENABLED = True\n")
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "sample-package"\nversion = "1.2.3"\n\n'
        "[project.scripts]\n"
        'sample-cli = "sample_package:main"\n\n'
        "[tool.hatch.build.targets.wheel]\n"
        'packages = ["src/sample_package"]\n'
    )
    return pyproject


def _write_wheel(path: Path, members: frozenset[str]) -> None:
    """Write one synthetic wheel package tree."""
    dist_info = path.name.removesuffix("-py3-none-any.whl") + ".dist-info"
    record_name = f"{dist_info}/RECORD"
    archive_members = members | {
        f"{dist_info}/METADATA",
        f"{dist_info}/WHEEL",
        record_name,
    }
    record = "".join(f"{member},,\n" for member in archive_members)
    with zipfile.ZipFile(path, "w") as archive:
        for member in archive_members:
            archive.writestr(member, record if member == record_name else "")


def _write_sdist(path: Path, members: frozenset[str]) -> None:
    """Write one synthetic source-distribution package tree."""
    archive_root = path.name.removesuffix(".tar.gz")
    with tarfile.open(path, mode="w:gz") as archive:
        for member in members:
            info = tarfile.TarInfo(f"{archive_root}/src/{member}")
            archive.addfile(info, io.BytesIO())


def test_runtime_dependencies_and_lock_match_reviewed_versions() -> None:
    """Direct selections and their native closure remain exactly reviewed."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    dependencies = set(pyproject["project"]["dependencies"])
    locked = {
        package["name"]: package.get("version")
        for package in tomllib.loads((REPO_ROOT / "uv.lock").read_text())[
            "package"
        ]
    }

    assert "click>=8.1" in dependencies
    assert "platformdirs==4.10.0" in dependencies
    assert "pydantic==2.13.4" in dependencies
    assert "portalocker==3.2.0" in dependencies
    assert "prompt-toolkit==3.0.52" in dependencies
    assert "pywin32==312; sys_platform == 'win32'" in dependencies
    assert all(
        not dependency.startswith("pywin32==312")
        or dependency.endswith("sys_platform == 'win32'")
        for dependency in dependencies
    )
    assert "urllib3==2.7.0" in dependencies
    assert set(pyproject["dependency-groups"]["dev"]) == {
        "ty>=0.0.35",
        "types-pywin32==312.0.0.20260609; sys_platform == 'win32'",
    }
    assert locked["portalocker"] == "3.2.0"
    assert locked["platformdirs"] == "4.10.0"
    assert locked["pydantic"] == "2.13.4"
    assert locked["pydantic-core"] == "2.46.4"
    assert locked["prompt-toolkit"] == "3.0.52"
    assert locked["pywin32"] == "312"
    assert locked["urllib3"] == "2.7.0"
    assert "B310" not in pyproject["tool"]["bandit"]["skips"]


def test_source_derived_artifact_contract(
    tmp_path: Path,
) -> None:
    """One build declaration governs source, wheel, and sdist membership."""
    pyproject = _write_project(tmp_path)
    contract = project.load_project_contract(tmp_path, pyproject)
    expected = frozenset(
        {
            "sample_package/__init__.py",
            "sample_package/feature.py",
        }
    )
    artifact_directory = tmp_path / "artifacts"
    artifact_directory.mkdir()
    wheel_name, sdist_name = artifacts.expected_artifact_names(contract)
    wheel = artifact_directory / wheel_name
    sdist = artifact_directory / sdist_name
    _write_wheel(wheel, expected)
    _write_sdist(sdist, expected)

    assert project.expected_package_members(contract) == expected
    assert artifacts.require_exact_distribution_set(
        contract,
        artifact_directory,
    ) == (wheel, sdist)
    project.verify_source_members(contract)
    artifacts.verify_wheel_members(contract, wheel)
    artifacts.verify_sdist_members(contract, sdist)

    unexpected = tmp_path / "src" / "sample_package" / "secret.dat"
    unexpected.write_text("not declared package data\n")
    with pytest.raises(
        WheelVerificationError,
        match="undeclared data",
    ):
        project.verify_source_members(contract)
    unexpected.unlink()

    extra = "sample_package/stale.py"
    _write_wheel(wheel, expected | {extra})
    _write_sdist(sdist, expected | {extra})
    with pytest.raises(WheelVerificationError):
        artifacts.verify_wheel_members(contract, wheel)
    with pytest.raises(WheelVerificationError):
        artifacts.verify_sdist_members(contract, sdist)

    project_source = pyproject.read_text()
    pyproject.write_text(
        project_source.replace(
            'sample-cli = "sample_package:main"\n',
            'sample-cli = "sample_package:main"\n'
            'CoDeX = "sample_package:main"\n',
        )
    )
    with pytest.raises(
        WheelVerificationError,
        match="replace provider commands",
    ):
        project.load_project_contract(tmp_path, pyproject)


def test_workflows_use_the_cross_platform_exact_wheel_verifier() -> None:
    """CI and publish share the verifier and contain no wheel glob install."""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    publish = (REPO_ROOT / ".github" / "workflows" / "publish.yml").read_text()

    assert "run: uv run python packaging/smoke_wheel.py --build" in ci
    assert (
        "uv run python packaging/smoke_wheel.py\n"
        "        --build --output-dir verified-dist" in ci
    )
    for operating_system, platform in (
        ("ubuntu-latest", "linux-x64"),
        ("macos-15", "macos-arm64"),
        ("macos-15-intel", "macos-x64"),
        ("windows-latest", "windows-x64"),
    ):
        assert (
            f"- os: {operating_system}\n          platform: {platform}"
        ) in ci
    assert "pytest with Unix pseudoterminal coverage" in ci
    assert "if: runner.os != 'Windows'" in ci
    assert "pytest with interactive mode disabled" in ci
    assert "if: runner.os == 'Windows'" in ci
    assert "--ignore=tests/dashboard/test_pty.py" in ci
    assert "os: [ubuntu-latest, macos-latest]" in ci
    assert (
        "Homebrew/actions/setup-homebrew@"
        "24728b77430f9659b8d89068e4afe7f5fd0f973c" in ci
    )
    assert "setup-sandbox: ${{ runner.os == 'Linux' }}" in ci
    assert "--source-archive /tmp/sidekick-usages-source.tar.gz" in ci
    assert "brew tap-new --no-git sidekick-usages/ci" in ci
    assert "$(brew --repository sidekick-usages/ci)/Formula/" in ci
    assert (
        "brew install --build-from-source\n"
        "        sidekick-usages/ci/sidekick-usages" in ci
    )
    assert "brew test sidekick-usages/ci/sidekick-usages" in ci
    assert "/tmp/sidekick-usages.rb" not in ci
    assert "packaging/smoke_wheel.py" in publish
    assert '--wheel "${{ steps.artifacts.outputs.wheel }}"' in publish
    assert "*.whl" not in ci
    assert "*.whl" not in publish

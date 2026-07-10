"""Release artifact and dependency packaging contracts."""

import importlib.util
import tomllib
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE_WHEEL_PATH = REPO_ROOT / "packaging" / "smoke_wheel.py"

spec = importlib.util.spec_from_file_location(
    "smoke_wheel",
    SMOKE_WHEEL_PATH,
)
assert spec is not None
smoke_wheel = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(smoke_wheel)


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
    assert "pydantic==2.13.4" in dependencies
    assert "urllib3==2.7.0" in dependencies
    assert locked["pydantic"] == "2.13.4"
    assert locked["pydantic-core"] == "2.46.4"
    assert locked["urllib3"] == "2.7.0"
    assert "B310" not in pyproject["tool"]["bandit"]["skips"]


def test_exact_wheel_selection_and_member_contract(tmp_path: Path) -> None:
    """Ambiguous artifacts and stale module/package collisions fail closed."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    wheel_name, sdist_name = smoke_wheel.expected_artifact_names()
    wheel = artifacts / wheel_name
    sdist = artifacts / sdist_name
    with zipfile.ZipFile(wheel, "w") as archive:
        for member in smoke_wheel.REQUIRED_WHEEL_MEMBERS:
            archive.writestr(member, "")
    sdist.touch()

    assert smoke_wheel.require_exact_wheel(artifacts) == wheel
    assert smoke_wheel.require_exact_distribution_set(artifacts) == (
        wheel,
        sdist,
    )
    smoke_wheel.verify_wheel_members(wheel)

    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("sidekick_usages/http.py", "")
    with pytest.raises(smoke_wheel.WheelVerificationError):
        smoke_wheel.verify_wheel_members(wheel)

    stale_wheel = artifacts / "stale-0.1.0-py3-none-any.whl"
    stale_wheel.touch()
    with pytest.raises(smoke_wheel.WheelVerificationError):
        smoke_wheel.require_exact_wheel(artifacts)
    stale_wheel.unlink()

    (artifacts / "unexpected.zip").touch()
    with pytest.raises(smoke_wheel.WheelVerificationError):
        smoke_wheel.require_exact_distribution_set(artifacts)


def test_workflows_use_the_cross_platform_exact_wheel_verifier() -> None:
    """CI and publish share the verifier and contain no wheel glob install."""
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    publish = (REPO_ROOT / ".github" / "workflows" / "publish.yml").read_text()

    assert "run: uv run python packaging/smoke_wheel.py --build" in ci
    assert (
        "uv run python packaging/smoke_wheel.py\n"
        "        --build --output-dir verified-dist" in ci
    )
    assert "os: [ubuntu-latest, macos-latest, windows-latest]" in ci
    assert "os: [ubuntu-latest, macos-latest]" in ci
    assert (
        "Homebrew/actions/setup-homebrew@"
        "24728b77430f9659b8d89068e4afe7f5fd0f973c" in ci
    )
    assert "setup-sandbox: ${{ runner.os == 'Linux' }}" in ci
    assert "--source-archive /tmp/sidekick-usages-source.tar.gz" in ci
    assert "brew install --build-from-source" in ci
    assert "brew test /tmp/sidekick-usages.rb" in ci
    assert "packaging/smoke_wheel.py" in publish
    assert '--wheel "${{ steps.artifacts.outputs.wheel }}"' in publish
    assert "*.whl" not in ci
    assert "*.whl" not in publish

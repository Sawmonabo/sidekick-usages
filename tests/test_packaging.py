"""Release artifact and dependency packaging contracts."""

import importlib.util
import io
import sys
import tarfile
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
    assert "platformdirs==4.10.0" in dependencies
    assert "pydantic==2.13.4" in dependencies
    assert "portalocker==3.2.0" in dependencies
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
    assert locked["pywin32"] == "312"
    assert locked["urllib3"] == "2.7.0"
    assert "B310" not in pyproject["tool"]["bandit"]["skips"]


def test_exact_wheel_selection_and_member_contract(tmp_path: Path) -> None:
    """All artifact forms require the package and reject flat remnants."""
    assert {
        ("claude", "setup-token", "--help"),
        ("codex", "login", "--help"),
        ("codex", "export", "--help"),
        ("setup-token", "--help"),
        ("codex-login", "--help"),
        ("codex-export", "--help"),
    } <= set(smoke_wheel.SMOKE_ARGUMENTS)
    assert {
        "sidekick_usages/heartbeat/base.py",
        "sidekick_usages/heartbeat/codex.py",
        "sidekick_usages/heartbeat/domain.py",
        "sidekick_usages/heartbeat/registry.py",
        "sidekick_usages/lifetime.py",
        "sidekick_usages/providers/codex.py",
        "sidekick_usages/store.py",
        "sidekick_usages/cli.py",
        "sidekick_usages/cli_help.py",
        "sidekick_usages/persistence/migration_errors.py",
        "sidekick_usages/persistence/migrations.py",
        "sidekick_usages/render.py",
        "sidekick_usages/token_input.py",
    } <= smoke_wheel.FORBIDDEN_WHEEL_MEMBERS
    assert {
        "sidekick_usages/credentials/codex.py",
        "sidekick_usages/credentials/models.py",
        "sidekick_usages/credentials/service.py",
        "sidekick_usages/heartbeat/models.py",
        "sidekick_usages/heartbeat/ports.py",
        "sidekick_usages/persistence/_compat/v060-reader.zip",
        "sidekick_usages/persistence/_platform/posix_private_bundles.py",
        "sidekick_usages/persistence/_platform/windows_private_bundles.py",
        "sidekick_usages/persistence/activity_snapshots.py",
        "sidekick_usages/persistence/credential_transaction_plans.py",
        "sidekick_usages/persistence/credential_transaction_recovery.py",
        "sidekick_usages/persistence/migrations/__init__.py",
        "sidekick_usages/persistence/migrations/account.py",
        "sidekick_usages/persistence/migrations/errors.py",
        "sidekick_usages/persistence/migrations/location.py",
        "sidekick_usages/persistence/migrations/observer.py",
        "sidekick_usages/persistence/migrations/ports.py",
        "sidekick_usages/persistence/migrations/service.py",
        "sidekick_usages/persistence/private_bundle_paths.py",
        "sidekick_usages/persistence/private_bundle_writes.py",
        "sidekick_usages/persistence/private_credential_contracts.py",
        "sidekick_usages/persistence/transaction.py",
        "sidekick_usages/providers/codex/auth.py",
        "sidekick_usages/providers/claude/activity.py",
        "sidekick_usages/providers/codex/activity.py",
        "sidekick_usages/providers/codex/auth_migration.py",
        "sidekick_usages/providers/codex/heartbeat.py",
        "sidekick_usages/providers/codex/provider.py",
        "sidekick_usages/providers/codex/request.py",
        "sidekick_usages/providers/codex/schemas.py",
        "sidekick_usages/providers/codex/usage.py",
        "sidekick_usages/providers/registry.py",
        "sidekick_usages/usage/activity.py",
        "sidekick_usages/usage/activity_render.py",
        "sidekick_usages/usage/narrow_render.py",
        "sidekick_usages/usage/render.py",
        "sidekick_usages/usage/reset_display.py",
        *smoke_wheel.REQUIRED_CLI_MEMBERS,
    } <= smoke_wheel.REQUIRED_WHEEL_MEMBERS
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
    smoke_wheel.verify_source_members()

    archive_root = sdist_name.removesuffix(".tar.gz")
    with tarfile.open(sdist, mode="w:gz") as archive:
        for member in smoke_wheel.REQUIRED_CLI_MEMBERS:
            info = tarfile.TarInfo(f"{archive_root}/src/{member}")
            archive.addfile(info, io.BytesIO())
    smoke_wheel.verify_sdist_members(sdist)

    with tarfile.open(sdist, mode="w:gz") as archive:
        for member in smoke_wheel.REQUIRED_CLI_MEMBERS | {
            "sidekick_usages/cli.py"
        }:
            info = tarfile.TarInfo(f"{archive_root}/src/{member}")
            archive.addfile(info, io.BytesIO())
    with pytest.raises(smoke_wheel.WheelVerificationError):
        smoke_wheel.verify_sdist_members(sdist)

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


def test_isolated_subprocess_preserves_unicode_output(
    tmp_path: Path,
) -> None:
    """Captured CLI output is explicitly UTF-8 at both pipe boundaries."""
    env = smoke_wheel._isolated_command_env(tmp_path / "absent-home")

    result = smoke_wheel._run(
        [sys.executable, "-c", 'print("┴ robot")'],
        cwd=tmp_path,
        env=env,
    )

    assert env["PYTHONIOENCODING"] == "utf-8"
    assert result.stdout == "┴ robot\n"


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

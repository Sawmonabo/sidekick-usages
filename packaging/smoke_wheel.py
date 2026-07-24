#!/usr/bin/env python3
"""Build and verify the exact sidekick-usages wheel in isolation."""

import argparse
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
SOURCE_EXCLUDED_DIRECTORIES = frozenset(
    {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
)

REQUIRED_CLI_MEMBERS = frozenset(
    {
        "sidekick_usages/cli/__init__.py",
        "sidekick_usages/cli/app.py",
        "sidekick_usages/cli/context.py",
        "sidekick_usages/cli/help.py",
        "sidekick_usages/cli/token_input.py",
        "sidekick_usages/cli/commands/__init__.py",
        "sidekick_usages/cli/commands/accounts.py",
        "sidekick_usages/cli/commands/claude.py",
        "sidekick_usages/cli/commands/codex.py",
        "sidekick_usages/cli/commands/credentials.py",
        "sidekick_usages/cli/commands/daemon.py",
        "sidekick_usages/cli/commands/doctor.py",
        "sidekick_usages/cli/commands/heartbeat.py",
        "sidekick_usages/cli/commands/maintenance.py",
        "sidekick_usages/cli/commands/migrate.py",
        "sidekick_usages/cli/commands/permissions.py",
        "sidekick_usages/cli/commands/updates.py",
        "sidekick_usages/cli/commands/usage.py",
    }
)

REQUIRED_WHEEL_MEMBERS = frozenset(
    {
        "sidekick_usages/__init__.py",
        "sidekick_usages/__main__.py",
        "sidekick_usages/branding.py",
        "sidekick_usages/clock.py",
        "sidekick_usages/core/__init__.py",
        "sidekick_usages/core/accounts/__init__.py",
        "sidekick_usages/core/accounts/models.py",
        "sidekick_usages/core/accounts/types.py",
        "sidekick_usages/core/accounts/validation.py",
        "sidekick_usages/credentials/__init__.py",
        "sidekick_usages/credentials/account_state.py",
        "sidekick_usages/credentials/authorities.py",
        "sidekick_usages/credentials/claude_lifetime.py",
        "sidekick_usages/credentials/claude_restore.py",
        "sidekick_usages/credentials/claude_setup_save.py",
        "sidekick_usages/credentials/claude_transitions.py",
        "sidekick_usages/credentials/codex.py",
        "sidekick_usages/credentials/models.py",
        "sidekick_usages/credentials/refresh.py",
        "sidekick_usages/credentials/service.py",
        "sidekick_usages/core/expiry.py",
        "sidekick_usages/core/models.py",
        "sidekick_usages/core/selection/__init__.py",
        "sidekick_usages/core/selection/models.py",
        "sidekick_usages/core/selection/policy.py",
        "sidekick_usages/core/selection/types.py",
        "sidekick_usages/core/time.py",
        "sidekick_usages/core/types.py",
        "sidekick_usages/daemon/__init__.py",
        "sidekick_usages/daemon/client.py",
        "sidekick_usages/daemon/control.py",
        "sidekick_usages/daemon/diagnostics.py",
        "sidekick_usages/daemon/dispatch.py",
        "sidekick_usages/daemon/entrypoint.py",
        "sidekick_usages/daemon/scheduled_maintenance.py",
        "sidekick_usages/daemon/models/__init__.py",
        "sidekick_usages/daemon/models/control.py",
        "sidekick_usages/daemon/models/diagnostics.py",
        "sidekick_usages/daemon/models/maintenance.py",
        "sidekick_usages/daemon/models/peer.py",
        "sidekick_usages/daemon/models/protocol.py",
        "sidekick_usages/daemon/models/scheduler.py",
        "sidekick_usages/daemon/models/service.py",
        "sidekick_usages/daemon/models/worker.py",
        "sidekick_usages/daemon/peer.py",
        "sidekick_usages/daemon/protocol.py",
        "sidekick_usages/daemon/recovery.py",
        "sidekick_usages/daemon/scheduler.py",
        "sidekick_usages/daemon/supervisor.py",
        "sidekick_usages/daemon/types/__init__.py",
        "sidekick_usages/daemon/types/control.py",
        "sidekick_usages/daemon/types/maintenance.py",
        "sidekick_usages/daemon/types/peer.py",
        "sidekick_usages/daemon/types/ports.py",
        "sidekick_usages/daemon/types/protocol.py",
        "sidekick_usages/daemon/types/service.py",
        "sidekick_usages/daemon/types/worker.py",
        "sidekick_usages/daemon/worker_entrypoint.py",
        "sidekick_usages/daemon/worker_runtime.py",
        "sidekick_usages/daemon/workers.py",
        "sidekick_usages/doctor.py",
        "sidekick_usages/doctor_credentials.py",
        "sidekick_usages/errors.py",
        "sidekick_usages/http/__init__.py",
        "sidekick_usages/http/client.py",
        "sidekick_usages/http/retry.py",
        "sidekick_usages/heartbeat/__init__.py",
        "sidekick_usages/heartbeat/models.py",
        "sidekick_usages/heartbeat/ports.py",
        "sidekick_usages/heartbeat/render.py",
        "sidekick_usages/heartbeat/service.py",
        "sidekick_usages/maintenance.py",
        "sidekick_usages/paths.py",
        "sidekick_usages/persistence/__init__.py",
        "sidekick_usages/persistence/_compat/v060-reader.zip",
        "sidekick_usages/persistence/_current_schema.py",
        "sidekick_usages/persistence/_platform/__init__.py",
        "sidekick_usages/persistence/_platform/macos.py",
        "sidekick_usages/persistence/_platform/macos_acl.py",
        "sidekick_usages/persistence/_platform/posix.py",
        "sidekick_usages/persistence/_platform/posix_files.py",
        "sidekick_usages/persistence/_platform/posix_mounts.py",
        "sidekick_usages/persistence/_platform/posix_namespace.py",
        "sidekick_usages/persistence/_platform/posix_private.py",
        "sidekick_usages/persistence/_platform/posix_private_bundles.py",
        "sidekick_usages/persistence/_platform/posix_private_platform.py",
        "sidekick_usages/persistence/_platform/posix_provider_stage.py",
        "sidekick_usages/persistence/_platform/windows.py",
        "sidekick_usages/persistence/_platform/windows_files.py",
        "sidekick_usages/persistence/_platform/windows_handles.py",
        "sidekick_usages/persistence/_platform/windows_namespace.py",
        "sidekick_usages/persistence/_platform/windows_private.py",
        "sidekick_usages/persistence/_platform/windows_private_bundles.py",
        "sidekick_usages/persistence/_platform/windows_private_tree.py",
        "sidekick_usages/persistence/activity_snapshots.py",
        "sidekick_usages/persistence/_platform/windows_security.py",
        "sidekick_usages/persistence/_recovery.py",
        "sidekick_usages/persistence/_schema_models.py",
        "sidekick_usages/persistence/_prototype_receipt_schema.py",
        "sidekick_usages/persistence/account_index.py",
        "sidekick_usages/persistence/account_runtime_bridge.py",
        "sidekick_usages/persistence/account_store.py",
        "sidekick_usages/persistence/account_store_support.py",
        "sidekick_usages/persistence/account_store_v3.py",
        "sidekick_usages/persistence/account_validation.py",
        "sidekick_usages/persistence/activation_journal.py",
        "sidekick_usages/persistence/artifacts.py",
        "sidekick_usages/persistence/assessment.py",
        "sidekick_usages/persistence/credential_authorities.py",
        "sidekick_usages/persistence/credential_refresh.py",
        "sidekick_usages/persistence/credential_refresh_artifacts.py",
        "sidekick_usages/persistence/credential_refresh_merge.py",
        "sidekick_usages/persistence/credential_refresh_private_stage.py",
        "sidekick_usages/persistence/schema/refresh.py",
        "sidekick_usages/persistence/credential_refresh_stage.py",
        "sidekick_usages/persistence/credential_ownership.py",
        "sidekick_usages/persistence/schema/transaction.py",
        "sidekick_usages/persistence/credential_transaction_plans.py",
        "sidekick_usages/persistence/credential_transaction_recovery.py",
        "sidekick_usages/persistence/credential_transactions.py",
        "sidekick_usages/persistence/errors.py",
        "sidekick_usages/persistence/filesystem.py",
        "sidekick_usages/persistence/filesystem_access.py",
        "sidekick_usages/persistence/inventory.py",
        "sidekick_usages/persistence/locking.py",
        "sidekick_usages/persistence/limits.py",
        "sidekick_usages/persistence/managed_migration.py",
        "sidekick_usages/persistence/managed_rollback.py",
        "sidekick_usages/persistence/migrations/__init__.py",
        "sidekick_usages/persistence/migrations/account.py",
        "sidekick_usages/persistence/migrations/account_codecs.py",
        "sidekick_usages/persistence/migrations/account_preview.py",
        "sidekick_usages/persistence/migrations/credential_kinds.py",
        "sidekick_usages/persistence/migrations/errors.py",
        "sidekick_usages/persistence/migrations/location.py",
        "sidekick_usages/persistence/migrations/location_state.py",
        "sidekick_usages/persistence/migrations/observer.py",
        "sidekick_usages/persistence/migrations/ports.py",
        "sidekick_usages/persistence/migrations/released_verification.py",
        "sidekick_usages/persistence/migrations/rollback.py",
        "sidekick_usages/persistence/migrations/service.py",
        "sidekick_usages/persistence/models/__init__.py",
        "sidekick_usages/persistence/models/account.py",
        "sidekick_usages/persistence/models/selection.py",
        "sidekick_usages/persistence/observations.py",
        "sidekick_usages/persistence/operation_queue.py",
        "sidekick_usages/persistence/operation_authority.py",
        "sidekick_usages/persistence/private_bundle_paths.py",
        "sidekick_usages/persistence/private_bundle_references.py",
        "sidekick_usages/persistence/private_bundle_writes.py",
        "sidekick_usages/persistence/private_credential_contracts.py",
        "sidekick_usages/persistence/private_credentials.py",
        "sidekick_usages/persistence/private_filesystem.py",
        "sidekick_usages/persistence/schema/__init__.py",
        "sidekick_usages/persistence/schema/account.py",
        "sidekick_usages/persistence/schemas.py",
        "sidekick_usages/persistence/schema/selection.py",
        "sidekick_usages/persistence/schema/service.py",
        "sidekick_usages/persistence/schema/worker.py",
        "sidekick_usages/persistence/selected_state.py",
        "sidekick_usages/persistence/service_state.py",
        "sidekick_usages/persistence/state_fields.py",
        "sidekick_usages/persistence/state_files.py",
        "sidekick_usages/persistence/state_filesystem.py",
        "sidekick_usages/persistence/state_json.py",
        "sidekick_usages/persistence/state_validation.py",
        "sidekick_usages/persistence/time_codec.py",
        "sidekick_usages/persistence/transaction.py",
        "sidekick_usages/persistence/transforms.py",
        "sidekick_usages/persistence/types/__init__.py",
        "sidekick_usages/persistence/types/activation.py",
        "sidekick_usages/persistence/types/inventory.py",
        "sidekick_usages/persistence/types/transaction.py",
        "sidekick_usages/persistence/v060.py",
        "sidekick_usages/persistence/worker_results.py",
        "sidekick_usages/providers/claude/__init__.py",
        "sidekick_usages/providers/claude/activity.py",
        "sidekick_usages/providers/claude/credential_schemas.py",
        "sidekick_usages/providers/claude/credentials.py",
        "sidekick_usages/providers/claude/heartbeat.py",
        "sidekick_usages/providers/claude/provider.py",
        "sidekick_usages/providers/claude/schemas.py",
        "sidekick_usages/providers/claude/usage.py",
        "sidekick_usages/providers/codex/__init__.py",
        "sidekick_usages/providers/codex/activity.py",
        "sidekick_usages/providers/codex/auth.py",
        "sidekick_usages/providers/codex/auth_migration.py",
        "sidekick_usages/providers/codex/heartbeat.py",
        "sidekick_usages/providers/codex/provider.py",
        "sidekick_usages/providers/codex/request.py",
        "sidekick_usages/providers/codex/schemas.py",
        "sidekick_usages/providers/codex/usage.py",
        "sidekick_usages/providers/__init__.py",
        "sidekick_usages/providers/base.py",
        "sidekick_usages/providers/registry.py",
        "sidekick_usages/scheduler_quiescence.py",
        "sidekick_usages/serialization/__init__.py",
        "sidekick_usages/serialization/json.py",
        "sidekick_usages/usage/__init__.py",
        "sidekick_usages/usage/activity.py",
        "sidekick_usages/usage/activity_render.py",
        "sidekick_usages/usage/narrow_render.py",
        "sidekick_usages/usage/models.py",
        "sidekick_usages/usage/render.py",
        "sidekick_usages/usage/reset_display.py",
        "sidekick_usages/usage/service.py",
        "sidekick_usages/update.py",
    }
    | REQUIRED_CLI_MEMBERS
)
FORBIDDEN_WHEEL_MEMBERS = frozenset(
    {
        "sidekick_usages/http.py",
        "sidekick_usages/lifetime.py",
        "sidekick_usages/render.py",
        "sidekick_usages/cli.py",
        "sidekick_usages/daemon.py",
        "sidekick_usages/cli_help.py",
        "sidekick_usages/token_input.py",
        "sidekick_usages/report.py",
        "sidekick_usages/store.py",
        "sidekick_usages/heartbeat/base.py",
        "sidekick_usages/heartbeat/claude.py",
        "sidekick_usages/heartbeat/codex.py",
        "sidekick_usages/heartbeat/domain.py",
        "sidekick_usages/heartbeat/registry.py",
        "sidekick_usages/providers/claude.py",
        "sidekick_usages/providers/codex.py",
        "sidekick_usages/persistence/migration_errors.py",
        "sidekick_usages/persistence/migrations.py",
    }
)

SMOKE_ARGUMENTS: tuple[tuple[str, ...], ...] = (
    ("--version",),
    ("--help",),
    ("daemon", "--help"),
    ("daemon", "status", "--help"),
    ("doctor", "--help"),
    ("add", "--help"),
    ("migrate", "locations", "--help"),
    ("claude", "--help"),
    ("claude", "setup-token", "--help"),
    ("claude", "restore-setup-token", "--help"),
    ("codex", "--help"),
    ("codex", "login", "--help"),
    ("codex", "export", "--help"),
    ("setup-token", "--help"),
    ("codex-login", "--help"),
    ("codex-export", "--help"),
)


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

    package_members = frozenset(
        member for member in members if member.startswith("sidekick_usages/")
    )
    missing = sorted(REQUIRED_WHEEL_MEMBERS - package_members)
    unexpected = sorted(package_members - REQUIRED_WHEEL_MEMBERS)
    forbidden = sorted(FORBIDDEN_WHEEL_MEMBERS & members)
    if missing or unexpected or forbidden:
        raise WheelVerificationError(
            f"Wheel member contract failed; missing={missing!r}, "
            f"unexpected={unexpected!r}, "
            f"forbidden={forbidden!r}."
        )


def verify_source_members() -> None:
    """Verify the checkout has the final package without flat remnants."""
    package_root = REPO_ROOT / "src"
    members = frozenset(
        path.relative_to(package_root).as_posix()
        for path in package_root.joinpath("sidekick_usages").rglob("*")
        if path.is_file()
        and not SOURCE_EXCLUDED_DIRECTORIES.intersection(path.parts)
    )
    missing = sorted(REQUIRED_WHEEL_MEMBERS - members)
    unexpected = sorted(members - REQUIRED_WHEEL_MEMBERS)
    forbidden = sorted(FORBIDDEN_WHEEL_MEMBERS & members)
    if missing or unexpected or forbidden:
        raise WheelVerificationError(
            f"Source member contract failed; missing={missing!r}, "
            f"unexpected={unexpected!r}, "
            f"forbidden={forbidden!r}."
        )


def verify_sdist_members(sdist: Path) -> None:
    """Verify the source distribution contains the same CLI contract."""
    archive_root = sdist.name.removesuffix(".tar.gz")
    prefix = f"{archive_root}/src/"
    try:
        with tarfile.open(sdist, mode="r:gz") as archive:
            members = frozenset(archive.getnames())
    except (OSError, tarfile.TarError) as error:
        raise WheelVerificationError(
            f"Invalid source distribution archive: {sdist}"
        ) from error
    required = frozenset(prefix + member for member in REQUIRED_WHEEL_MEMBERS)
    package_prefix = prefix + "sidekick_usages/"
    package_members = frozenset(
        member for member in members if member.startswith(package_prefix)
    )
    forbidden_contract = frozenset(
        prefix + member for member in FORBIDDEN_WHEEL_MEMBERS
    )
    missing = sorted(required - members)
    unexpected = sorted(package_members - required)
    forbidden = sorted(forbidden_contract & members)
    if missing or unexpected or forbidden:
        raise WheelVerificationError(
            f"Source distribution member contract failed; "
            f"missing={missing!r}, unexpected={unexpected!r}, "
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
            "import importlib.metadata, pathlib, sidekick_usages, sys; "
            "import sidekick_usages.cli.app; "
            "import sidekick_usages.cli.context; "
            "import sidekick_usages.persistence.filesystem; "
            "import sidekick_usages.persistence.locking; "
            "import sidekick_usages.persistence.private_credentials; "
            "import sidekick_usages.persistence.transaction; "
            "origin = pathlib.Path(sidekick_usages.__file__).resolve(); "
            "prefix = pathlib.Path(sys.prefix).resolve(); "
            "dependency = pathlib.Path("
            "sys.modules['platformdirs'].__file__).resolve(); "
            "assert origin.is_relative_to(prefix), (origin, prefix); "
            "assert dependency.is_relative_to(prefix), "
            "(dependency, prefix); "
            "assert importlib.metadata.version('platformdirs') == '4.10.0'"
        )
        _run(
            [str(python), "-c", origin_check],
            cwd=run_dir,
            env=env,
        )

        compatibility_check = """
from datetime import UTC, datetime
from pathlib import Path

from sidekick_usages.core.expiry import KnownExpiry, UnknownExpiry
from sidekick_usages.core.models import Account, ClaudeLoginCredentials
from sidekick_usages.core.types import AccountLabel
from sidekick_usages.persistence.artifacts import (
    FileFingerprint,
    FileIdentity,
    FileSnapshot,
    sha256_digest,
)
from sidekick_usages.persistence.schemas import encode_generation_zero
from sidekick_usages.persistence.transforms import (
    accounts_to_version_one,
    version_one_to_v060,
)
from sidekick_usages.persistence.v060 import ReleasedV060Verifier

account = Account(
    label=AccountLabel("claude-wheel-测试"),
    credentials=ClaudeLoginCredentials(
        access_token="test-only-wheel-access",
        refresh_token="test-only-wheel-refresh",
        access_expiry=KnownExpiry(datetime(2027, 1, 1, tzinfo=UTC)),
        refresh_expiry=UnknownExpiry(),
        scopes=("user:profile",),
    ),
    plan="team",
)
payload = encode_generation_zero(
    version_one_to_v060(accounts_to_version_one((account,)))
)
authority = Path("synthetic-accounts.json").resolve()
authority.write_bytes(payload)
metadata = authority.stat()
expected = FileSnapshot(
    FileFingerprint(
        FileIdentity(metadata.st_dev, metadata.st_ino),
        sha256_digest(payload),
        len(payload),
    ),
    metadata.st_nlink,
    payload,
)
verifier = ReleasedV060Verifier()
verifier.preflight()
verifier.verify(authority, expected)
authority.unlink()
"""
        _run(
            [str(python), "-c", compatibility_check],
            cwd=run_dir,
            env=env,
        )

        entry_points = (
            (str(console),),
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

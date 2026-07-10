"""Claude Code platform credential discovery."""

import os
import platform
import subprocess
from pathlib import Path

from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    DetectedCredentials,
)
from sidekick_usages.errors import InvalidPayloadError, UsageError
from sidekick_usages.providers.claude.schemas import parse_credentials_blob
from sidekick_usages.serialization import decode_json_object


def detect_credentials(
    credential_home: Path | None = None,
) -> DetectedCredentials | None:
    """Read credentials from the current platform's Claude install."""
    del credential_home
    system = platform.system()
    if system == "Darwin":
        return _from_macos_keychain()
    if system == "Linux":
        return _from_linux_files()
    if system == "Windows":
        return _from_windows()
    return None


def require_claude_credentials(account: Account) -> ClaudeCredentials:
    """Return Claude credentials or reject an incompatible account."""
    credentials = account.credentials
    if isinstance(credentials, ClaudeCredentials):
        return credentials
    raise UsageError(f"Account {account.label!r} is not a Claude account.")


def _from_macos_keychain() -> DetectedCredentials | None:
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                "Claude Code-credentials",
                "-w",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        return parse_credentials_blob(
            decode_json_object(result.stdout.strip().encode("utf-8"))
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        InvalidPayloadError,
    ):
        return None


def _from_linux_files() -> DetectedCredentials | None:
    for path in (
        Path.home() / ".claude" / ".credentials.json",
        Path.home() / ".config" / "claude" / ".credentials.json",
    ):
        if not path.exists():
            continue
        try:
            return parse_credentials_blob(
                decode_json_object(path.read_bytes())
            )
        except InvalidPayloadError:
            continue
    return None


def _from_windows() -> DetectedCredentials | None:
    appdata = Path(os.environ.get("APPDATA", ""))
    for path in (
        Path.home() / ".claude" / ".credentials.json",
        appdata / "Claude" / ".credentials.json",
    ):
        if not path.exists():
            continue
        try:
            return parse_credentials_blob(
                decode_json_object(path.read_bytes())
            )
        except InvalidPayloadError:
            continue
    try:
        ps_script = (
            "$c = Get-StoredCredential "
            "-Target 'Claude Code-credentials' "
            "-ErrorAction SilentlyContinue; "
            "if ($c) { $c.GetNetworkCredential().Password }"
        )
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        powershell_bin = (
            rf"{system_root}\System32"
            r"\WindowsPowerShell\v1.0\powershell.exe"
        )
        result = subprocess.run(
            [powershell_bin, "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        out = result.stdout.strip()
        if out:
            return parse_credentials_blob(
                decode_json_object(out.encode("utf-8"))
            )
    except (
        subprocess.SubprocessError,
        InvalidPayloadError,
        FileNotFoundError,
    ):
        pass
    return None

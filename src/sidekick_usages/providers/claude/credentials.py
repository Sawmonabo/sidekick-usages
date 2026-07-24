"""Claude Code platform credential discovery."""

import os
import platform
import subprocess
from datetime import datetime
from pathlib import Path

from sidekick_usages.core.expiry import (
    ExpiredExpiry,
    classify_expiry,
)
from sidekick_usages.core.models import (
    Account,
    ClaudeCredentials,
    ClaudeLoginCredentials,
    ClaudeSetupTokenCredentials,
)
from sidekick_usages.errors import InvalidPayloadError
from sidekick_usages.providers.base import (
    CredentialDetection,
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureCause,
    ProviderFailureKind,
)
from sidekick_usages.providers.claude.schema.credentials import (
    parse_credentials_blob,
)
from sidekick_usages.providers.claude.schema.usage import (
    claude_failure,
)
from sidekick_usages.serialization import decode_json_object

_MAX_CREDENTIAL_BYTES = 1024 * 1024
_KEYCHAIN_ITEM_NOT_FOUND_EXIT = (-25300) % 256


def detect_credentials(
    reference_time: datetime,
    credential_home: Path | None = None,
) -> CredentialDetection:
    """Read and classify credentials from the platform Claude install."""
    del credential_home
    system = platform.system()
    if system == "Darwin":
        return _from_macos_keychain(reference_time)
    if system == "Linux":
        return _from_linux_files(reference_time)
    if system == "Windows":
        return _from_windows(reference_time)
    return _missing_credentials()


def require_claude_credentials(account: Account) -> ClaudeCredentials:
    """Return Claude credentials or reject an incompatible account."""
    credentials = account.credentials
    if isinstance(
        credentials,
        ClaudeSetupTokenCredentials | ClaudeLoginCredentials,
    ):
        return credentials
    raise ProviderBoundaryError(
        claude_failure(
            ProviderFailureKind.IDENTITY_MISMATCH,
            "The saved account does not contain Claude credentials.",
        )
    ) from None


def _from_macos_keychain(reference_time: datetime) -> CredentialDetection:
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
    except subprocess.CalledProcessError as error:
        if error.returncode == _KEYCHAIN_ITEM_NOT_FOUND_EXIT:
            return _missing_credentials()
        return _unreadable_credentials()
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
        subprocess.TimeoutExpired,
    ):
        return _unreadable_credentials()
    try:
        payload = result.stdout.strip().encode("utf-8")
    except UnicodeEncodeError:
        return claude_failure(
            ProviderFailureKind.MALFORMED,
            "Claude credential data is not valid UTF-8.",
        )
    return parse_detected_credentials(payload, reference_time)


def _from_linux_files(reference_time: datetime) -> CredentialDetection:
    try:
        home = Path.home()
    except OSError, RuntimeError:
        return _unreadable_credentials()
    return _from_files(
        (
            home / ".claude" / ".credentials.json",
            home / ".config" / "claude" / ".credentials.json",
        ),
        reference_time,
    )


def _from_windows(reference_time: datetime) -> CredentialDetection:
    try:
        home = Path.home()
    except OSError, RuntimeError:
        return _unreadable_credentials()
    paths = [home / ".claude" / ".credentials.json"]
    if appdata := os.environ.get("APPDATA"):
        paths.append(Path(appdata) / "Claude" / ".credentials.json")
    file_result = _from_files(
        tuple(paths),
        reference_time,
    )
    if not (
        isinstance(file_result, ProviderFailure)
        and file_result.kind is ProviderFailureKind.MISSING
    ):
        return file_result
    return _from_windows_credential_manager(reference_time)


def _from_windows_credential_manager(
    reference_time: datetime,
) -> CredentialDetection:
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
    except (
        FileNotFoundError,
        OSError,
        subprocess.SubprocessError,
        subprocess.TimeoutExpired,
    ):
        return _unreadable_credentials()
    if result.returncode != 0:
        return _unreadable_credentials()
    output = result.stdout.strip()
    if not output:
        return _missing_credentials()
    try:
        payload = output.encode("utf-8")
    except UnicodeEncodeError:
        return claude_failure(
            ProviderFailureKind.MALFORMED,
            "Claude credential data is not valid UTF-8.",
        )
    return parse_detected_credentials(payload, reference_time)


def _from_files(
    paths: tuple[Path, ...],
    reference_time: datetime,
) -> CredentialDetection:
    for path in paths:
        candidate = read_credentials_path(path, reference_time)
        if (
            isinstance(candidate, ProviderFailure)
            and candidate.kind is ProviderFailureKind.MISSING
        ):
            continue
        return candidate
    return _missing_credentials()


def read_credentials_path(
    path: Path,
    reference_time: datetime,
) -> CredentialDetection:
    """Read and classify one concrete Claude credential file."""
    try:
        path.stat()
    except FileNotFoundError:
        return _missing_credentials()
    except OSError:
        return _unreadable_credentials()
    try:
        payload = _read_bounded(path)
    except OSError:
        return _unreadable_credentials()
    except ValueError:
        return claude_failure(
            ProviderFailureKind.MALFORMED,
            "Claude credential data exceeds the supported size.",
        )
    return parse_detected_credentials(payload, reference_time)


def _read_bounded(path: Path) -> bytes:
    with path.open("rb") as stream:
        payload = stream.read(_MAX_CREDENTIAL_BYTES + 1)
    if len(payload) > _MAX_CREDENTIAL_BYTES:
        raise ValueError
    return payload


def parse_detected_credentials(
    payload: bytes,
    reference_time: datetime,
) -> CredentialDetection:
    """Decode, validate, and classify one Claude credential payload."""
    try:
        blob = decode_json_object(payload)
    except InvalidPayloadError:
        return claude_failure(
            ProviderFailureKind.MALFORMED,
            "Claude credential data is not valid JSON.",
        )
    try:
        detected = parse_credentials_blob(blob)
    except ProviderBoundaryError as error:
        return error.failure
    credentials = detected.credentials
    if not isinstance(credentials, ClaudeLoginCredentials):
        raise AssertionError(
            "Claude native parsing returned setup credentials."
        )
    if isinstance(
        classify_expiry(
            credentials.refresh_expiry,
            now=reference_time,
        ),
        ExpiredExpiry,
    ):
        return claude_failure(
            ProviderFailureKind.EXPIRED,
            "The saved Claude login credential has expired.",
            cause=ProviderFailureCause.LOGIN_CREDENTIAL_EXPIRED,
        )
    if isinstance(
        classify_expiry(credentials.access_expiry, now=reference_time),
        ExpiredExpiry,
    ):
        return claude_failure(
            ProviderFailureKind.EXPIRED,
            "The saved Claude access credential has expired.",
            cause=ProviderFailureCause.ACCESS_CREDENTIAL_EXPIRED,
        )
    return detected


def _missing_credentials() -> ProviderFailure:
    return claude_failure(
        ProviderFailureKind.MISSING,
        "Claude credentials were not found. Log in with Claude Code.",
    )


def _unreadable_credentials() -> ProviderFailure:
    return claude_failure(
        ProviderFailureKind.UNREADABLE,
        "Claude credentials could not be read. Check access and retry.",
    )

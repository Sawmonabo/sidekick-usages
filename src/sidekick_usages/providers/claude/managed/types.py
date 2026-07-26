"""Closed managed-Claude capability types."""

from enum import StrEnum


class ClaudeManagedPlatform(StrEnum):
    """Supported Claude host and protected-storage combinations."""

    LINUX_FILE = "linux_file"
    WSL_FILE = "wsl_file"
    MACOS_ARM64_KEYCHAIN = "macos_arm64_keychain"
    MACOS_X64_KEYCHAIN = "macos_x64_keychain"


class ClaudeManagedFailure(StrEnum):
    """Safe reasons managed Claude authentication is unavailable."""

    FEATURE_DISABLED = "feature_disabled"
    PLATFORM_UNSUPPORTED = "platform_unsupported"
    PROFILE_UNSAFE = "profile_unsafe"
    EXECUTABLE_MISSING = "executable_missing"
    EXECUTABLE_UNSAFE = "executable_unsafe"
    VERSION_UNSUPPORTED = "version_unsupported"
    STATUS_UNSUPPORTED = "status_unsupported"
    LOGIN_UNSUPPORTED = "login_unsupported"
    REFRESH_PROVISIONING_UNPROVEN = "refresh_provisioning_unproven"
    OFFICIAL_LOGIN_TIMED_OUT = "official_login_timed_out"
    OFFICIAL_LOGIN_UNAVAILABLE = "official_login_unavailable"
    OFFICIAL_LOGIN_UNVERIFIED = "official_login_unverified"

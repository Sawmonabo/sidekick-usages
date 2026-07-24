"""Closed scalar types for resident service state."""

from enum import StrEnum

_MAX_PACKAGE_VERSION_BYTES = 128
_MIN_PRINTABLE_ASCII = 0x21
_MAX_PRINTABLE_ASCII = 0x7E

__all__ = [
    "PackageVersion",
    "ServicePhase",
]


class PackageVersion(str):
    """Bounded printable installed-package version."""

    def __new__(cls, value: str) -> PackageVersion:
        if not isinstance(value, str):
            raise TypeError("Package version must be a string.")
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError(
                "Package version must be printable ASCII."
            ) from None
        if (
            not encoded
            or len(encoded) > _MAX_PACKAGE_VERSION_BYTES
            or any(
                character < _MIN_PRINTABLE_ASCII
                or character > _MAX_PRINTABLE_ASCII
                for character in encoded
            )
        ):
            raise ValueError("Package version must be printable ASCII.")
        return super().__new__(cls, value)


class ServicePhase(StrEnum):
    """Closed resident supervisor lifecycle phases."""

    STARTING = "starting"
    RECOVERING = "recovering"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"

"""Closed local control endpoint failure types."""

from enum import StrEnum


class EndpointFailureCode(StrEnum):
    """Safe local socket lifecycle failures."""

    CREATE_FAILED = "create_failed"
    FEATURE_DISABLED = "feature_disabled"
    UNSAFE_RUNTIME_DIRECTORY = "unsafe_runtime_directory"
    UNSAFE_SOCKET_PATH = "unsafe_socket_path"
    SOCKET_IN_USE = "socket_in_use"

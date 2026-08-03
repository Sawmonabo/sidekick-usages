"""Closed local control endpoint failure types."""

from enum import StrEnum


class EndpointFailureCode(StrEnum):
    """Safe local socket lifecycle failures."""

    CREATE_FAILED = "create_failed"
    FEATURE_DISABLED = "feature_disabled"
    UNSAFE_RUNTIME_DIRECTORY = "unsafe_runtime_directory"
    UNSAFE_SOCKET_PATH = "unsafe_socket_path"
    SOCKET_IN_USE = "socket_in_use"


class ControlFailurePhase(StrEnum):
    """Sanitized local control failures owned by supervisor diagnostics."""

    DISPATCH = "control_dispatch_failed"
    SUBSCRIPTION_CANCELLATION = "subscription_cancellation_failed"

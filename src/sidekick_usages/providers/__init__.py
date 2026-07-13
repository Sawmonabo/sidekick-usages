"""Shared provider contract and runtime result types."""

from sidekick_usages.core.models import DetectedCredentials
from sidekick_usages.providers.base import (
    CredentialDetection,
    Provider,
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureCause,
    ProviderFailureKind,
    RefreshResult,
    RefreshSuccess,
)

__all__ = [
    "CredentialDetection",
    "DetectedCredentials",
    "Provider",
    "ProviderBoundaryError",
    "ProviderFailure",
    "ProviderFailureCause",
    "ProviderFailureKind",
    "RefreshResult",
    "RefreshSuccess",
]

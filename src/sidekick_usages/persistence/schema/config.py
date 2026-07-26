"""Shared configuration for strict persistence boundary models."""

from pydantic import ConfigDict

STRICT_SCHEMA_CONFIG = ConfigDict(
    strict=True,
    extra="forbid",
    frozen=True,
)

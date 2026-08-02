"""Canonical identifiers shared by infrastructure-free core owners."""

from typing import ClassVar, Self
from uuid import UUID


class CanonicalUuid(str):
    """Canonical lower-case UUID identifier."""

    _name: ClassVar[str]

    def __new__(cls, value: str) -> Self:
        """Validate and construct one canonical UUID string."""
        if not isinstance(value, str):
            raise TypeError(f"{cls._name} must be a string.")
        try:
            parsed = UUID(value)
        except ValueError, AttributeError, TypeError:
            raise ValueError(
                f"{cls._name} must be a canonical UUID."
            ) from None
        if str(parsed) != value:
            raise ValueError(f"{cls._name} must be a canonical UUID.")
        return super().__new__(cls, value)

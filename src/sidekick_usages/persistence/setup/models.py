"""Validated guided service-setup acknowledgement."""

from dataclasses import dataclass

from sidekick_usages.daemon.types.protocol import MAX_PROTOCOL_VERSION


@dataclass(frozen=True, slots=True)
class ServiceSetupAcknowledgement:
    """One approved resident control protocol generation."""

    protocol_generation: int

    def __post_init__(self) -> None:
        """Require one positive integer protocol generation."""
        if (
            type(self.protocol_generation) is not int
            or not 1 <= self.protocol_generation <= MAX_PROTOCOL_VERSION
        ):
            raise ValueError("Service setup protocol generation is invalid.")

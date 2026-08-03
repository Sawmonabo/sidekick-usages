"""Release-gated ownership of one protected Claude session host."""

from typing import Protocol

from sidekick_usages.cli.session.models import (
    SessionLaunchError,
    SessionLaunchFailure,
)
from sidekick_usages.core.selection.policy import (
    protected_selection_enabled,
)
from sidekick_usages.core.types import ProviderId


class ClaudeStructuredHost(Protocol):
    """Run one qualified structured Claude host event loop."""

    def run(self, arguments: tuple[str, ...]) -> int:
        """Return the official engine's natural exit status."""


class ClaudeCliSession:
    """Keep the protected host inaccessible until every release gate passes."""

    def __init__(self, host: ClaudeStructuredHost | None = None) -> None:
        self._host = host

    def run(self, arguments: tuple[str, ...]) -> int:
        """Run only a fully qualified and explicitly enabled host."""
        host = self._host
        if not protected_selection_enabled(ProviderId.CLAUDE) or host is None:
            raise SessionLaunchError(
                SessionLaunchFailure.UNSUPPORTED,
                "Claude protected session integration remains disabled; "
                "the provider process was not started.",
            )
        return host.run(arguments)

"""Synthetic-only qualification boundary for Codex model attempts."""

from collections.abc import Callable

from sidekick_usages.core.accounts.types import ProviderIdentity
from sidekick_usages.providers.codex.session.models import (
    CodexSessionCapability,
)


class SyntheticCodexModelAttempt:
    """Prove HTTP/current-auth selection without real model transport."""

    def __init__(
        self,
        capability: CodexSessionCapability,
        current_auth: Callable[[], ProviderIdentity],
    ) -> None:
        self._capability = capability
        self._current_auth = current_auth
        self._auth_resolutions = 0
        self._http_accounts: list[ProviderIdentity] = []
        self._websocket_opens = 0

    @property
    def auth_resolutions(self) -> int:
        """Return the number of per-attempt current-auth resolutions."""
        return self._auth_resolutions

    @property
    def http_accounts(self) -> tuple[ProviderIdentity, ...]:
        """Return identities observed by the synthetic HTTP spy."""
        return tuple(self._http_accounts)

    @property
    def websocket_opens(self) -> int:
        """Return attempted synthetic model WebSocket opens."""
        return self._websocket_opens

    def attempt(self) -> None:
        """Resolve current auth and select the qualified model boundary."""
        if not self._capability.supported:
            raise AssertionError("Synthetic model attempt is not qualified.")
        self._auth_resolutions += 1
        identity = self._current_auth()
        if self._capability.supports_websockets:
            self._websocket_opens += 1
            return
        self._http_accounts.append(identity)

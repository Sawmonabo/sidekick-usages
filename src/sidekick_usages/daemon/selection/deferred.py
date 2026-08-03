"""Provider-neutral deferred selection authority boundary."""

from sidekick_usages.core.selection.models import (
    AuthorityReadyProof,
    FinalizedSelection,
    OpenSelectionOperation,
    PreparedSelection,
)
from sidekick_usages.core.selection.types import SelectionCode
from sidekick_usages.daemon.selection.coordinator import SelectionRequestError


class DeferredSelectionAuthority:
    """Refuse provider work until a qualified provider adapter is composed."""

    def prevalidate(
        self,
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
    ) -> PreparedSelection:
        """Refuse selection without changing provider authority."""
        del operation, baseline
        raise SelectionRequestError(SelectionCode.PROVIDER_UNAVAILABLE)

    def commit(self, prepared: PreparedSelection) -> AuthorityReadyProof:
        """Refuse authority mutation without a qualified provider adapter."""
        del prepared
        raise SelectionRequestError(SelectionCode.PROVIDER_UNAVAILABLE)

    def readback(
        self,
        prepared: PreparedSelection,
    ) -> AuthorityReadyProof | None:
        """Return no proof without inspecting provider-owned state."""
        del prepared
        return None

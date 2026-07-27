"""Infrastructure-free interactive dashboard transitions."""

from dataclasses import dataclass, replace

from sidekick_usages.cli.dashboard.models.controller import (
    ActivateOrRepairIntent,
    ClaudeAssociationRequest,
    DashboardActivationProof,
    DashboardControllerState,
    DashboardMove,
    DashboardProviderAnchor,
    RefreshAccountIntent,
    RefreshDueAccountsIntent,
)
from sidekick_usages.core.accounts.types import CredentialHealth
from sidekick_usages.core.selection.types import ProviderRuntimeState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.usage.dashboard.focus import (
    initial_dashboard_cursor,
    provider_focus,
)
from sidekick_usages.usage.dashboard.models import (
    DashboardAccount,
    DashboardActionState,
    DashboardExternalRow,
    DashboardProvider,
    DashboardRow,
    DashboardSnapshot,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardController:
    """Apply pure cursor and action transitions to one dashboard snapshot."""

    snapshot: DashboardSnapshot
    state: DashboardControllerState

    @classmethod
    def start(cls, snapshot: DashboardSnapshot) -> DashboardController:
        """Create initial focus from provider-verified dashboard state."""
        anchors = tuple(
            _provider_anchor(provider)
            for provider in snapshot.providers
            if provider.rows
        )
        cursor = initial_dashboard_cursor(snapshot)
        if cursor.focused_provider is None:
            state = DashboardControllerState(
                focused_provider=None,
                account_id=None,
                external=False,
                anchors=anchors,
            )
        else:
            state = _state_at_anchor(
                anchors,
                _find_anchor(anchors, cursor.focused_provider),
            )
        return cls(snapshot=snapshot, state=state)

    def move(self, direction: DashboardMove) -> DashboardController:
        """Move the preview cursor one clamped row without changing truth."""
        provider = self._focused_provider()
        if provider is None:
            return self
        if self.state.account_id is None and not self.state.external:
            target = (
                provider.rows[-1]
                if direction is DashboardMove.UP
                else provider.rows[0]
            )
            return self._focus(target)
        current_index = _focused_index(provider, self.state)
        if direction is DashboardMove.UP:
            target_index = max(0, current_index - 1)
        else:
            target_index = min(len(provider.rows) - 1, current_index + 1)
        return self._focus(provider.rows[target_index])

    def focus_next_provider(self) -> DashboardController:
        """Focus the next non-empty provider at its restore anchor."""
        providers = tuple(
            provider for provider in self.snapshot.providers if provider.rows
        )
        if not providers:
            return self
        provider_ids = tuple(provider.provider_id for provider in providers)
        if self.state.focused_provider not in provider_ids:
            target = providers[0]
        else:
            current_index = provider_ids.index(self.state.focused_provider)
            target = providers[(current_index + 1) % len(providers)]
        anchor = _find_anchor(self.state.anchors, target.provider_id)
        return self._with_state(
            _state_at_anchor(
                self.state.anchors,
                anchor,
                help_visible=self.state.help_visible,
            )
        )

    def restore(self) -> DashboardController:
        """Restore the focused provider to verified-active or first row."""
        if self.state.focused_provider is None:
            return self
        anchor = _find_anchor(
            self.state.anchors,
            self.state.focused_provider,
        )
        return self._with_state(
            _state_at_anchor(
                self.state.anchors,
                anchor,
                help_visible=self.state.help_visible,
            )
        )

    def toggle_help(self) -> DashboardController:
        """Toggle concise keyboard help without changing cursor state."""
        return self._with_state(
            replace(
                self.state,
                help_visible=not self.state.help_visible,
            )
        )

    def activate_or_repair(
        self,
    ) -> ActivateOrRepairIntent | ClaudeAssociationRequest | None:
        """Return a saved-account mutation intent when actions are enabled."""
        focused = self._focused_account()
        if focused is None:
            return None
        provider, account = focused
        setup_association = (
            provider.provider_id is ProviderId.CLAUDE
            and DashboardActionState.SWITCH_SETUP_REQUIRED in account.states
        )
        if setup_association:
            if (
                not provider.actions_enabled
                or provider.runtime_state is None
                or provider.runtime_state
                in {
                    ProviderRuntimeState.UNREADABLE,
                    ProviderRuntimeState.UNSUPPORTED,
                }
            ):
                return None
            return ClaudeAssociationRequest(account_id=account.account_id)
        if provider.provider_id is ProviderId.CLAUDE and (
            provider.runtime_state is not ProviderRuntimeState.SAVED_ACTIVE
            or provider.active_account_id is None
        ):
            return None
        if not self._mutations_enabled(provider):
            return None
        return ActivateOrRepairIntent(
            provider_id=provider.provider_id,
            account_id=account.account_id,
        )

    def refresh_account(self) -> RefreshAccountIntent | None:
        """Return a one-account refresh intent without changing selection."""
        focused = self._focused_account()
        if focused is None:
            return None
        provider, account = focused
        if not self._mutations_enabled(provider):
            return None
        return RefreshAccountIntent(
            provider_id=provider.provider_id,
            account_id=account.account_id,
        )

    def refresh_due_accounts(self) -> RefreshDueAccountsIntent | None:
        """Return a global due-refresh intent when an account is due."""
        actionable = any(
            self._mutations_enabled(provider)
            and any(
                isinstance(row, DashboardAccount)
                and row.credential_health is CredentialHealth.REFRESH_DUE
                for row in provider.rows
            )
            for provider in self.snapshot.providers
        )
        return RefreshDueAccountsIntent() if actionable else None

    def activation_succeeded(
        self,
        proof: DashboardActivationProof,
    ) -> DashboardController:
        """Adopt only a service-proven saved account as the restore target."""
        provider = self._provider(proof.provider_id)
        if provider is None:
            raise ValueError("Activation proof provider is not displayed.")
        account = next(
            (
                row
                for row in provider.rows
                if isinstance(row, DashboardAccount)
                and row.account_id == proof.account_id
            ),
            None,
        )
        if account is None:
            raise ValueError("Activation proof account is not displayed.")
        if (
            provider.active_account_id != proof.account_id
            or not account.active
        ):
            raise ValueError(
                "Activation proof contradicts provider read-back."
            )
        proven_anchor = DashboardProviderAnchor(
            provider_id=proof.provider_id,
            account_id=proof.account_id,
            external=False,
        )
        anchors = tuple(
            (
                proven_anchor
                if anchor.provider_id is proof.provider_id
                else anchor
            )
            for anchor in self.state.anchors
        )
        return self._with_state(
            _state_at_anchor(
                anchors,
                proven_anchor,
                help_visible=self.state.help_visible,
            )
        )

    def rebase(
        self,
        snapshot: DashboardSnapshot,
        *,
        restore_provider: ProviderId | None = None,
    ) -> DashboardController:
        """Adopt fresh cached truth while preserving one valid preview."""
        verified = DashboardController.start(snapshot)
        anchors = verified.state.anchors
        if restore_provider is not None:
            anchor = next(
                (
                    candidate
                    for candidate in anchors
                    if candidate.provider_id is restore_provider
                ),
                None,
            )
            if anchor is not None:
                return DashboardController(
                    snapshot=snapshot,
                    state=_state_at_anchor(
                        anchors,
                        anchor,
                        help_visible=self.state.help_visible,
                    ),
                )
        if _focused_at_anchor(self.state):
            anchor = next(
                (
                    candidate
                    for candidate in anchors
                    if candidate.provider_id is self.state.focused_provider
                ),
                None,
            )
            if anchor is not None:
                return DashboardController(
                    snapshot=snapshot,
                    state=_state_at_anchor(
                        anchors,
                        anchor,
                        help_visible=self.state.help_visible,
                    ),
                )
        focused = _matching_row(snapshot, self.state)
        if focused is not None:
            state = DashboardControllerState(
                focused_provider=focused.provider_id,
                account_id=(
                    focused.account_id
                    if isinstance(focused, DashboardAccount)
                    else None
                ),
                external=isinstance(focused, DashboardExternalRow),
                anchors=anchors,
                help_visible=self.state.help_visible,
            )
            return DashboardController(snapshot=snapshot, state=state)
        return DashboardController(
            snapshot=snapshot,
            state=replace(
                verified.state,
                help_visible=self.state.help_visible,
            ),
        )

    def _focus(self, row: DashboardRow) -> DashboardController:
        provider_id = row.provider_id
        return self._with_state(
            DashboardControllerState(
                focused_provider=provider_id,
                account_id=(
                    row.account_id
                    if isinstance(row, DashboardAccount)
                    else None
                ),
                external=isinstance(row, DashboardExternalRow),
                anchors=self.state.anchors,
                help_visible=self.state.help_visible,
            )
        )

    def _focused_account(
        self,
    ) -> tuple[DashboardProvider, DashboardAccount] | None:
        provider = self._focused_provider()
        if (
            provider is None
            or self.state.account_id is None
            or self.state.external
        ):
            return None
        account = next(
            (
                row
                for row in provider.rows
                if isinstance(row, DashboardAccount)
                and row.account_id == self.state.account_id
            ),
            None,
        )
        return None if account is None else (provider, account)

    def _focused_provider(self) -> DashboardProvider | None:
        provider_id = self.state.focused_provider
        return None if provider_id is None else self._provider(provider_id)

    def _provider(self, provider_id: ProviderId) -> DashboardProvider | None:
        return next(
            (
                provider
                for provider in self.snapshot.providers
                if provider.provider_id is provider_id
            ),
            None,
        )

    def _mutations_enabled(self, provider: DashboardProvider) -> bool:
        if provider.actions_enabled:
            return True
        return (
            not self.snapshot.service.ready
            and provider.runtime_state
            not in {
                ProviderRuntimeState.UNREADABLE,
                ProviderRuntimeState.UNSUPPORTED,
            }
        )

    def _with_state(
        self,
        state: DashboardControllerState,
    ) -> DashboardController:
        return replace(self, state=state)


def _provider_anchor(
    provider: DashboardProvider,
) -> DashboardProviderAnchor:
    focus = provider_focus(provider)
    return DashboardProviderAnchor(
        provider_id=provider.provider_id,
        account_id=focus.account_id,
        external=focus.external,
    )


def _matching_row(
    snapshot: DashboardSnapshot,
    state: DashboardControllerState,
) -> DashboardRow | None:
    if state.focused_provider is None:
        return None
    provider = next(
        (
            candidate
            for candidate in snapshot.providers
            if candidate.provider_id is state.focused_provider
        ),
        None,
    )
    if provider is None:
        return None
    return next(
        (
            row
            for row in provider.rows
            if (isinstance(row, DashboardExternalRow) and state.external)
            or (
                isinstance(row, DashboardAccount)
                and not state.external
                and row.account_id == state.account_id
            )
        ),
        None,
    )


def _find_anchor(
    anchors: tuple[DashboardProviderAnchor, ...],
    provider_id: ProviderId,
) -> DashboardProviderAnchor:
    return next(
        anchor for anchor in anchors if anchor.provider_id is provider_id
    )


def _state_at_anchor(
    anchors: tuple[DashboardProviderAnchor, ...],
    anchor: DashboardProviderAnchor,
    *,
    help_visible: bool = False,
) -> DashboardControllerState:
    return DashboardControllerState(
        focused_provider=anchor.provider_id,
        account_id=anchor.account_id,
        external=anchor.external,
        anchors=anchors,
        help_visible=help_visible,
    )


def _focused_index(
    provider: DashboardProvider,
    state: DashboardControllerState,
) -> int:
    for index, row in enumerate(provider.rows):
        if isinstance(row, DashboardAccount):
            if row.account_id == state.account_id and not state.external:
                return index
        elif state.external:
            return index
    return 0


def _focused_at_anchor(state: DashboardControllerState) -> bool:
    provider_id = state.focused_provider
    if provider_id is None:
        return False
    anchor = next(
        (
            candidate
            for candidate in state.anchors
            if candidate.provider_id is provider_id
        ),
        None,
    )
    return (
        anchor is not None
        and anchor.account_id == state.account_id
        and anchor.external == state.external
    )

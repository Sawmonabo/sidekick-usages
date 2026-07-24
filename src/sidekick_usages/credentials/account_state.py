"""Credential-free saved-account state updates."""

from dataclasses import replace

from sidekick_usages.core.models import Account
from sidekick_usages.persistence.accounts.store import AccountStore
from sidekick_usages.persistence.errors import SourceChangedError


def persist_provider_plan_without_credentials(
    store: AccountStore,
    current: Account,
    candidate: Account,
) -> bool:
    """Persist a plan-only provider update through the no-secret index."""
    if candidate.credentials != current.credentials:
        return False
    if candidate.plan == current.plan:
        return True
    saved_accounts = store.saved_accounts()
    saved = next(
        (
            item
            for item in saved_accounts
            if item.provider_id is candidate.provider_id
            and item.label == candidate.label
        ),
        None,
    )
    if saved is None:
        raise SourceChangedError
    store.persist_state(
        replace(saved, plan=candidate.plan),
        expected=saved,
    )
    return True

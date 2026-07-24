"""Validated account references to private provider bundles."""

from collections.abc import Iterable
from pathlib import Path

from sidekick_usages.core.models import Account
from sidekick_usages.persistence.errors import (
    PrivateCredentialCollisionError,
)
from sidekick_usages.persistence.private.credentials import (
    PrivateCredentialTree,
)
from sidekick_usages.persistence.types.credential import (
    PrivateCredentialOwnership,
)


def canonical_private_accounts(
    accounts: Iterable[Account],
    private: PrivateCredentialTree,
) -> dict[Path, Account]:
    """Index accounts by their unique canonical private auth home."""
    references: dict[Path, Account] = {}
    for account in accounts:
        auth_home = account.codex_home
        if auth_home is None:
            continue
        path = Path(auth_home)
        if (
            private.classify_bundle(path)
            is PrivateCredentialOwnership.CANONICAL
        ):
            if path in references:
                raise PrivateCredentialCollisionError(path.name)
            references[path] = account
    return references


__all__ = ["canonical_private_accounts"]

"""One-way credential-authority generation primitives."""

import hashlib

from sidekick_usages.core.accounts.types import AuthorityGeneration


def hashed_authority_generation(
    value: str,
    *,
    prefix: str,
) -> AuthorityGeneration:
    """Return a provider-namespaced one-way generation."""
    if not value or not prefix or not prefix.endswith(":"):
        raise ValueError("Authority generation input is invalid.")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Authority generation input is invalid.") from None
    return AuthorityGeneration(prefix + hashlib.sha256(encoded).hexdigest())

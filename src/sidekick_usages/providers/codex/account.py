"""Strict managed Codex account reads over the app server."""

from sidekick_usages.core.accounts.validation import (
    MAX_METADATA_BYTES,
    require_bounded_text,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.ports import (
    JsonRpcRequester,
)
from sidekick_usages.providers.codex.models import CodexAccountObservation
from sidekick_usages.serialization.json import JsonValue

ACCOUNT_READ_METHOD = "account/read"


def read_codex_account(
    session: JsonRpcRequester,
    *,
    refresh_token: bool,
) -> CodexAccountObservation | ProviderFailure:
    """Read one non-null ChatGPT account with optional official refresh."""
    result = session.request(
        ACCOUNT_READ_METHOD,
        {"refreshToken": refresh_token},
    )
    if (
        set(result) != {"account", "requiresOpenaiAuth"}
        or type(result.get("requiresOpenaiAuth")) is not bool
    ):
        return _failure(
            ProviderFailureKind.MALFORMED,
            "Codex returned malformed account metadata.",
        )
    return _account_observation(result.get("account"))


def _account_observation(
    account: JsonValue | None,
) -> CodexAccountObservation | ProviderFailure:
    if account is None:
        return _failure(
            ProviderFailureKind.MISSING,
            "The managed Codex home is logged out.",
        )
    if not isinstance(account, dict) or set(account) != {
        "email",
        "planType",
        "type",
    }:
        return _failure(
            ProviderFailureKind.MALFORMED,
            "Codex returned malformed account metadata.",
        )
    account_type = account.get("type")
    if account_type != "chatgpt":
        return _failure(
            ProviderFailureKind.UNSUPPORTED,
            "The managed Codex home is not a ChatGPT account.",
        )
    email = account.get("email")
    plan = account.get("planType")
    if (email is not None and not isinstance(email, str)) or not isinstance(
        plan, str
    ):
        return _failure(
            ProviderFailureKind.MALFORMED,
            "Codex returned malformed account metadata.",
        )
    try:
        if email is not None:
            require_bounded_text(
                email,
                name="Codex account email",
                maximum=MAX_METADATA_BYTES,
            )
        require_bounded_text(
            plan,
            name="Codex plan",
            maximum=MAX_METADATA_BYTES,
        )
    except TypeError, ValueError:
        return _failure(
            ProviderFailureKind.MALFORMED,
            "Codex returned malformed account metadata.",
        )
    return CodexAccountObservation(plan)


def _failure(
    kind: ProviderFailureKind,
    message: str,
) -> ProviderFailure:
    return ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=kind,
        message=message,
    )

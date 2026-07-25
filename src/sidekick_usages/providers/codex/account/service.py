"""Strict managed Codex account reads over the app server."""

from sidekick_usages.core.accounts.validation import (
    MAX_METADATA_BYTES,
    require_bounded_text,
)
from sidekick_usages.providers.codex.account.types import (
    CodexAccountReadFailure,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.ports import (
    DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    JsonRpcRequester,
)
from sidekick_usages.providers.codex.models import CodexAccountObservation
from sidekick_usages.serialization.json import JsonValue

ACCOUNT_READ_METHOD = "account/read"


def read_codex_account(
    session: JsonRpcRequester,
    *,
    refresh_token: bool,
    timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
) -> CodexAccountObservation | CodexAccountReadFailure:
    """Read one non-null ChatGPT account with optional official refresh."""
    result = session.request(
        ACCOUNT_READ_METHOD,
        {"refreshToken": refresh_token},
        timeout_seconds=timeout_seconds,
    )
    if (
        set(result) != {"account", "requiresOpenaiAuth"}
        or type(result.get("requiresOpenaiAuth")) is not bool
    ):
        return CodexAccountReadFailure.MALFORMED
    return _account_observation(result.get("account"))


def _account_observation(
    account: JsonValue | None,
) -> CodexAccountObservation | CodexAccountReadFailure:
    if account is None:
        return CodexAccountReadFailure.MISSING
    if not isinstance(account, dict) or set(account) != {
        "email",
        "planType",
        "type",
    }:
        return CodexAccountReadFailure.MALFORMED
    if account.get("type") != "chatgpt":
        return CodexAccountReadFailure.UNSUPPORTED
    email = account.get("email")
    plan = account.get("planType")
    if (email is not None and not isinstance(email, str)) or not isinstance(
        plan, str
    ):
        return CodexAccountReadFailure.MALFORMED
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
        return CodexAccountReadFailure.MALFORMED
    return CodexAccountObservation(plan)

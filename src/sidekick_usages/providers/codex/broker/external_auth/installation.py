"""Strict external-auth installation in the shared Codex daemon."""

import time

from sidekick_usages.providers.codex.account.service import read_codex_account
from sidekick_usages.providers.codex.account.types import (
    CodexAccountReadFailure,
)
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.models import (
    JsonRpcNotification,
    JsonRpcServerRequest,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.external_auth.refresh import (
    CODEX_REFRESH_ERROR_CODE,
    CODEX_REFRESH_ERROR_MESSAGE,
    CODEX_REFRESH_METHOD,
)
from sidekick_usages.providers.codex.broker.models import (
    CodexProjectionReceipt,
)
from sidekick_usages.providers.codex.broker.ports import CodexProjection
from sidekick_usages.providers.codex.broker.types import CodexBrokerFailure
from sidekick_usages.providers.codex.broker.wire import CodexDaemonSession
from sidekick_usages.providers.codex.token import decode_codex_token_claims
from sidekick_usages.serialization.json import JsonObject

ACCOUNT_LOGIN_START_METHOD = "account/login/start"
ACCOUNT_LOGIN_COMPLETED_METHOD = "account/login/completed"
ACCOUNT_UPDATED_METHOD = "account/updated"
EXTERNAL_AUTH_TYPE = "chatgptAuthTokens"
_INSTALL_TIMEOUT_SECONDS = 8.0
_MAX_INSTALL_MESSAGES = 16


def install_codex_projection(
    session: CodexDaemonSession,
    projection: CodexProjection,
    *,
    deadline: float | None = None,
) -> CodexProjectionReceipt:
    """Install and corroborate one locally identity-bound projection."""
    effective_deadline = (
        time.monotonic() + _INSTALL_TIMEOUT_SECONDS
        if deadline is None
        else deadline
    )
    expected_identity = str(projection.provider_identity)
    try:
        claimed_identity = decode_codex_token_claims(
            projection.access_token
        ).provider_identity
    except TypeError, ValueError:
        raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH) from None
    if claimed_identity != projection.provider_identity:
        raise CodexBrokerError(CodexBrokerFailure.IDENTITY_MISMATCH)
    params: JsonObject = {
        "accessToken": projection.access_token,
        "chatgptAccountId": expected_identity,
        "chatgptPlanType": projection.plan,
        "type": EXTERNAL_AUTH_TYPE,
    }
    try:
        result = session.request(
            ACCOUNT_LOGIN_START_METHOD,
            params,
            timeout_seconds=_remaining(effective_deadline),
        )
    finally:
        params.clear()
    if result != {"type": EXTERNAL_AUTH_TYPE}:
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
    _require_external_auth_update(
        session,
        projection.plan,
        effective_deadline,
    )
    observed = read_codex_account(
        session,
        refresh_token=False,
        timeout_seconds=_remaining(effective_deadline),
    )
    if isinstance(observed, CodexAccountReadFailure):
        if observed is CodexAccountReadFailure.MALFORMED:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
        raise CodexBrokerError(CodexBrokerFailure.PROJECTION_REJECTED)
    if observed.plan != projection.plan:
        raise CodexBrokerError(CodexBrokerFailure.PROJECTION_REJECTED)
    authority = session.authority
    return CodexProjectionReceipt(
        account_id=projection.account_id,
        provider_identity=projection.provider_identity,
        generation=projection.generation,
        plan=observed.plan,
        socket_device=authority.control_socket.device,
        socket_inode=authority.control_socket.inode,
    )


def _require_external_auth_update(
    session: CodexDaemonSession,
    expected_plan: str,
    deadline: float,
) -> None:
    completed = False
    for _message_index in range(_MAX_INSTALL_MESSAGES):
        remaining = _remaining(deadline)
        message = session.receive(timeout_seconds=remaining)
        if isinstance(message, JsonRpcServerRequest):
            if message.method == CODEX_REFRESH_METHOD:
                session.respond_error(
                    message.request_id,
                    CODEX_REFRESH_ERROR_CODE,
                    CODEX_REFRESH_ERROR_MESSAGE,
                    timeout_seconds=remaining,
                )
            continue
        if not isinstance(message, JsonRpcNotification):
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
        if message.method == ACCOUNT_LOGIN_COMPLETED_METHOD:
            _require_external_completion(message.params)
            completed = True
            continue
        if message.method != ACCOUNT_UPDATED_METHOD:
            continue
        if not completed:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
        _require_external_update(message.params, expected_plan)
        return
    raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)


def _require_external_completion(params: JsonObject) -> None:
    if (
        set(params) != {"error", "loginId", "success"}
        or params.get("loginId") is not None
        or params.get("success") is not True
        or params.get("error") is not None
    ):
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)


def _require_external_update(
    params: JsonObject,
    expected_plan: str,
) -> None:
    plan = params.get("planType")
    if (
        set(params) != {"authMode", "planType"}
        or params.get("authMode") != EXTERNAL_AUTH_TYPE
        or plan != expected_plan
    ):
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_TIMEOUT)
    return remaining

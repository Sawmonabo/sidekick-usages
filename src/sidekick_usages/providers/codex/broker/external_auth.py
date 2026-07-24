"""Strict external-auth installation in the shared Codex daemon."""

import time

from sidekick_usages.providers.base import (
    ProviderBoundaryError,
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.account import read_codex_account
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.models import (
    JsonRpcNotification,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.broker.errors import CodexBrokerError
from sidekick_usages.providers.codex.broker.models import (
    CodexProjectionReceipt,
)
from sidekick_usages.providers.codex.broker.types import (
    CodexBrokerFailure,
    CodexProjection,
)
from sidekick_usages.providers.codex.broker.wire import CodexDaemonSession
from sidekick_usages.providers.codex.schemas import account_id_from_token
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
) -> CodexProjectionReceipt:
    """Install and corroborate one locally identity-bound projection."""
    expected_identity = str(projection.provider_identity)
    try:
        claimed_identity = account_id_from_token(projection.access_token)
    except ProviderBoundaryError:
        raise CodexBrokerError(
            CodexBrokerFailure.IDENTITY_MISMATCH
        ) from None
    if claimed_identity != expected_identity:
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
            timeout_seconds=_INSTALL_TIMEOUT_SECONDS,
        )
    finally:
        params.clear()
    if result != {"type": EXTERNAL_AUTH_TYPE}:
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
    _require_external_auth_update(session, projection.plan)
    observed = read_codex_account(session, refresh_token=False)
    if isinstance(observed, ProviderFailure):
        if observed.kind is ProviderFailureKind.MALFORMED:
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_MALFORMED
            )
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
) -> None:
    deadline = time.monotonic() + _INSTALL_TIMEOUT_SECONDS
    completed = False
    for _message_index in range(_MAX_INSTALL_MESSAGES):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_TIMEOUT)
        message = session.receive(timeout_seconds=remaining)
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

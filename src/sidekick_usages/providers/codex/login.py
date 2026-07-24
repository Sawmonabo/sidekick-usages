"""Official final-home Codex login over the app server."""

import time

from sidekick_usages.core.accounts.validation import (
    MAX_METADATA_BYTES,
    require_bounded_text,
)
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.base import (
    ProviderFailure,
    ProviderFailureKind,
)
from sidekick_usages.providers.codex.account import read_codex_account
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.models import (
    JsonRpcNotification,
)
from sidekick_usages.providers.codex.app_server.session import (
    CodexAppServerSession,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.models import (
    CodexAccountObservation,
    CodexLoginAttempt,
    CodexLoginEvent,
)
from sidekick_usages.serialization.json import JsonObject

ACCOUNT_LOGIN_START_METHOD = "account/login/start"
ACCOUNT_LOGIN_COMPLETED_METHOD = "account/login/completed"
ACCOUNT_UPDATED_METHOD = "account/updated"
_BROWSER_LOGIN_TIMEOUT_SECONDS = 630.0
_DEVICE_LOGIN_TIMEOUT_SECONDS = 930.0
_LOGIN_START_TIMEOUT_SECONDS = 30.0
_MAX_LOGIN_MESSAGES = 64


def start_codex_login(
    session: CodexAppServerSession,
    *,
    device_auth: bool,
) -> CodexLoginAttempt:
    """Start official ChatGPT login and return its ephemeral user step."""
    login_type = "chatgptDeviceCode" if device_auth else "chatgpt"
    result = session.request(
        ACCOUNT_LOGIN_START_METHOD,
        {"type": login_type},
        timeout_seconds=_LOGIN_START_TIMEOUT_SECONDS,
    )
    return _login_attempt(
        result,
        expected_type=login_type,
        device_auth=device_auth,
    )


def complete_codex_login(
    session: CodexAppServerSession,
    attempt: CodexLoginAttempt,
) -> CodexAccountObservation | ProviderFailure:
    """Wait for matching completion and authenticated account update."""
    timeout = (
        _DEVICE_LOGIN_TIMEOUT_SECONDS
        if attempt.event.user_code is not None
        else _BROWSER_LOGIN_TIMEOUT_SECONDS
    )
    deadline = time.monotonic() + timeout
    completed = False
    for _ in range(_MAX_LOGIN_MESSAGES):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_TIMEOUT)
        message = session.receive(
            timeout_seconds=remaining,
        )
        if not isinstance(message, JsonRpcNotification):
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
        if message.method == ACCOUNT_LOGIN_COMPLETED_METHOD:
            matched, failure = _matching_completion(
                message.params,
                expected_login_id=attempt.login_id,
            )
            if not matched:
                continue
            if failure is not None:
                return failure
            completed = True
            continue
        if message.method != ACCOUNT_UPDATED_METHOD or not completed:
            continue
        updated = _authenticated_update(message.params)
        if isinstance(updated, ProviderFailure):
            return updated
        return read_codex_account(session, refresh_token=False)
    raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)


def _login_attempt(
    result: JsonObject,
    *,
    expected_type: str,
    device_auth: bool,
) -> CodexLoginAttempt:
    if result.get("type") != expected_type:
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
    login_id = result.get("loginId")
    authorization_url = (
        result.get("verificationUrl")
        if expected_type == "chatgptDeviceCode"
        else result.get("authUrl")
    )
    user_code = (
        result.get("userCode")
        if expected_type == "chatgptDeviceCode"
        else None
    )
    if not isinstance(login_id, str) or not isinstance(
        authorization_url,
        str,
    ):
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
    if device_auth:
        if not isinstance(user_code, str):
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
        validated_user_code = user_code
    else:
        validated_user_code = None
    try:
        event = CodexLoginEvent(
            authorization_url=authorization_url,
            user_code=validated_user_code,
        )
        return CodexLoginAttempt(
            login_id=login_id,
            event=event,
        )
    except TypeError, ValueError:
        raise CodexAppServerError(
            CodexAppServerFailure.PROTOCOL_MALFORMED
        ) from None


def _matching_completion(
    params: JsonObject,
    *,
    expected_login_id: str,
) -> tuple[bool, ProviderFailure | None]:
    login_id = params.get("loginId")
    success = params.get("success")
    error = params.get("error")
    if (
        not isinstance(login_id, str)
        or type(success) is not bool
        or (error is not None and not isinstance(error, str))
    ):
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
    if error is not None:
        try:
            require_bounded_text(
                error,
                name="Codex login error",
                maximum=MAX_METADATA_BYTES,
            )
        except TypeError, ValueError:
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_MALFORMED
            ) from None
    if login_id != expected_login_id:
        return False, None
    if success:
        if error is not None:
            raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
        return True, None
    return True, ProviderFailure(
        provider_id=ProviderId.CODEX,
        kind=ProviderFailureKind.REJECTED,
        message="Codex login was cancelled or rejected.",
    )


def _authenticated_update(
    params: JsonObject,
) -> bool | ProviderFailure:
    auth_mode = params.get("authMode")
    plan = params.get("planType")
    if (auth_mode is not None and not isinstance(auth_mode, str)) or (
        plan is not None and not isinstance(plan, str)
    ):
        raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)
    if auth_mode != "chatgpt":
        return ProviderFailure(
            provider_id=ProviderId.CODEX,
            kind=ProviderFailureKind.REJECTED,
            message="Codex completed sign-in without a ChatGPT account.",
        )
    if plan is not None:
        try:
            require_bounded_text(
                plan,
                name="Codex plan",
                maximum=MAX_METADATA_BYTES,
            )
        except TypeError, ValueError:
            raise CodexAppServerError(
                CodexAppServerFailure.PROTOCOL_MALFORMED
            ) from None
    return True

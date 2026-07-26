"""Strict effective Codex authentication observation."""

from datetime import datetime
from typing import Never

from sidekick_usages.core.selection.models import ProviderAuthObservation
from sidekick_usages.core.selection.types import ProviderAuthState
from sidekick_usages.core.types import ProviderId
from sidekick_usages.providers.codex.app_server.errors import (
    CodexAppServerError,
)
from sidekick_usages.providers.codex.app_server.jsonrpc.ports import (
    DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
    JsonRpcRequester,
)
from sidekick_usages.providers.codex.app_server.methods import (
    AUTH_STATUS_METHOD,
)
from sidekick_usages.providers.codex.app_server.types import (
    CodexAppServerFailure,
)
from sidekick_usages.providers.codex.auth.token import (
    codex_access_token_generation,
    decode_codex_token_claims,
)
from sidekick_usages.serialization.json import JsonObject

CHATGPT_AUTH_METHOD = "chatgpt"


def probe_codex_auth_status(
    session: JsonRpcRequester,
    *,
    timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
) -> None:
    """Prove the unexported auth-status request without reading a token."""
    result = _request_auth_status(
        session,
        include_token=False,
        timeout_seconds=timeout_seconds,
    )
    try:
        if result != {
            "authMethod": None,
            "authToken": None,
            "requiresOpenaiAuth": True,
        }:
            _malformed()
    finally:
        result.clear()


def observe_codex_auth_status(
    session: JsonRpcRequester,
    *,
    observed_at: datetime,
    timeout_seconds: float = DEFAULT_JSON_RPC_TIMEOUT_SECONDS,
) -> ProviderAuthObservation:
    """Return the effective login without retaining its access token."""
    result = _request_auth_status(
        session,
        include_token=True,
        timeout_seconds=timeout_seconds,
    )
    try:
        if set(result) != {
            "authMethod",
            "authToken",
            "requiresOpenaiAuth",
        }:
            _malformed()
        auth_method = result.get("authMethod")
        token = result.get("authToken")
        requires_auth = result.get("requiresOpenaiAuth")
        if requires_auth is not True:
            return _inactive_observation(
                ProviderAuthState.UNSUPPORTED,
                observed_at,
            )
        if auth_method is None and token is None:
            return _inactive_observation(
                ProviderAuthState.LOGGED_OUT,
                observed_at,
            )
        if auth_method != CHATGPT_AUTH_METHOD or not isinstance(token, str):
            return _inactive_observation(
                ProviderAuthState.UNSUPPORTED,
                observed_at,
            )
        claims = decode_codex_token_claims(token)
        if claims.provider_identity is None:
            _malformed()
        generation = codex_access_token_generation(token)
        return ProviderAuthObservation(
            provider_id=ProviderId.CODEX,
            state=ProviderAuthState.ACTIVE,
            provider_identity=claims.provider_identity,
            generation=generation,
            observed_at=observed_at,
        )
    except UnicodeEncodeError, ValueError:
        _malformed()
    finally:
        result.clear()


def _request_auth_status(
    session: JsonRpcRequester,
    *,
    include_token: bool,
    timeout_seconds: float,
) -> JsonObject:
    return session.request(
        AUTH_STATUS_METHOD,
        {
            "includeToken": include_token,
            "refreshToken": False,
        },
        timeout_seconds=timeout_seconds,
    )


def _inactive_observation(
    state: ProviderAuthState,
    observed_at: datetime,
) -> ProviderAuthObservation:
    return ProviderAuthObservation(
        provider_id=ProviderId.CODEX,
        state=state,
        provider_identity=None,
        generation=None,
        observed_at=observed_at,
    )


def _malformed() -> Never:
    raise CodexAppServerError(CodexAppServerFailure.PROTOCOL_MALFORMED)

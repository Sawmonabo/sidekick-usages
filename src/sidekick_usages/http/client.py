"""Pooled HTTPS transport behind the Sidekick HTTP facade."""

import json
import random
import re
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from http import HTTPMethod, HTTPStatus
from threading import Lock
from types import TracebackType

import urllib3
from urllib3.util import Timeout

from sidekick_usages.clock import Clock, SystemClock
from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    HttpStatusError,
    InsecureUrlError,
    InvalidPayloadError,
    TransientError,
)
from sidekick_usages.http.models import HttpAttempt
from sidekick_usages.http.retry import (
    CONNECT_TIMEOUT_SECONDS,
    READ_TIMEOUT_SECONDS,
    RetryExecutor,
)
from sidekick_usages.http.types import HttpOperation
from sidekick_usages.serialization.json import (
    JsonObject,
    decode_json_object,
)

JSON_REQUEST_LIMIT = 1024 * 1024
JSON_RESPONSE_LIMIT = 4 * 1024 * 1024
DISCARD_BODY_LIMIT = 64 * 1024
ERROR_BODY_LIMIT = 64 * 1024
_MAX_PORT_EXCLUSIVE = 1 << 16

_SCOPE_REQUIREMENT_RE = re.compile(
    r"scope requirement ([A-Za-z0-9:_-]{1,128})"
)
_SAFE_REQUIRED_SCOPES = frozenset({"user:profile"})

type _PoolManager = urllib3.PoolManager | urllib3.ProxyManager


class HttpClient(AbstractContextManager["HttpClient"]):
    """Invocation-scoped pooled HTTPS client with fixed retry policy."""

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
    ) -> None:
        self._retry = RetryExecutor(
            clock=clock or SystemClock(),
            monotonic=monotonic,
            sleep=sleep,
            random_source=random_source,
        )
        self._direct_manager: urllib3.PoolManager | None = None
        self._proxy_manager: urllib3.ProxyManager | None = None
        self._proxy_url: str | None = None
        self._retired_managers: list[_PoolManager] = []
        self._active_requests = 0
        self._closed = False
        self._transport_lock = Lock()

    def __enter__(self) -> HttpClient:
        """Return this client without eagerly creating a pool."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close every initialized pool exactly once."""
        del exc_type, exc_value, traceback
        self.close()

    def close(self) -> None:
        """Close initialized connection pools idempotently."""
        with self._transport_lock:
            if self._closed:
                return
            self._closed = True
            managers = (
                self._detach_managers()
                if self._active_requests == 0
                else ()
            )
        _clear_managers(managers)

    def get_json(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> JsonObject:
        """GET and decode one bounded JSON-object response."""
        result = self._request(
            HTTPMethod.GET,
            url,
            headers,
            body=None,
            operation=HttpOperation.SAFE_READ,
            response_limit=JSON_RESPONSE_LIMIT,
        )
        return decode_json_object(result.body)

    def post_capture_headers(
        self,
        url: str,
        json_body: JsonObject,
        headers: Mapping[str, str],
        *,
        operation: HttpOperation,
    ) -> dict[str, str]:
        """POST JSON and return normalized response headers."""
        _require_operation(
            operation,
            HttpOperation.CLAUDE_PROBE,
            HttpOperation.CLAUDE_HEARTBEAT,
            HttpOperation.CODEX_HEARTBEAT,
        )
        body = _encode_json(json_body)
        request_headers = {"Content-Type": "application/json", **headers}
        result = self._request(
            HTTPMethod.POST,
            url,
            request_headers,
            body=body,
            operation=operation,
            response_limit=DISCARD_BODY_LIMIT,
            discard_oversized=True,
        )
        return result.headers

    def post_json(
        self,
        url: str,
        json_body: JsonObject,
        headers: Mapping[str, str] | None = None,
        *,
        operation: HttpOperation,
    ) -> JsonObject:
        """POST bounded JSON and decode a JSON-object response."""
        _require_operation(operation, HttpOperation.CLAUDE_REFRESH)
        body = _encode_json(json_body)
        request_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if headers is not None:
            request_headers.update(headers)
        result = self._request(
            HTTPMethod.POST,
            url,
            request_headers,
            body=body,
            operation=operation,
            response_limit=JSON_RESPONSE_LIMIT,
        )
        return decode_json_object(result.body)

    def _request(
        self,
        method: HTTPMethod,
        url: str,
        headers: Mapping[str, str],
        *,
        body: bytes | None,
        operation: HttpOperation,
        response_limit: int,
        discard_oversized: bool = False,
    ) -> HttpAttempt:
        """Execute and translate one operation through the retry owner."""
        _require_https(url)

        def attempt(remaining: float) -> HttpAttempt:
            return self._attempt(
                method,
                url,
                headers,
                body,
                remaining,
                response_limit,
                discard_oversized,
            )

        result = self._retry.execute(operation, attempt)
        status = result.status_code
        if HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
            return result
        if status == HTTPStatus.UNAUTHORIZED:
            raise AuthError("Token expired or invalid (HTTP 401).") from None
        if status == HTTPStatus.FORBIDDEN:
            raise _forbidden_error(result.body) from None
        raise HttpStatusError(status) from None

    def _attempt(
        self,
        method: HTTPMethod,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        remaining: float,
        response_limit: int,
        discard_oversized: bool,
    ) -> HttpAttempt:
        """Perform one retry-disabled, redirect-disabled request."""
        manager = self._acquire_manager(url)
        try:
            timeout = Timeout(
                total=remaining,
                connect=min(CONNECT_TIMEOUT_SECONDS, remaining),
                read=min(READ_TIMEOUT_SECONDS, remaining),
            )
            response = manager.request(
                method,
                url,
                body=body,
                headers=headers,
                timeout=timeout,
                preload_content=False,
                decode_content=False,
                redirect=False,
                retries=False,
            )
            response_headers = {
                str(key).lower(): str(value)
                for key, value in response.headers.items()
            }
            limit = (
                response_limit
                if HTTPStatus.OK
                <= response.status
                < HTTPStatus.MULTIPLE_CHOICES
                else ERROR_BODY_LIMIT
            )
            payload = _read_bounded(
                response,
                limit,
                discard_oversized
                or not (
                    HTTPStatus.OK
                    <= response.status
                    < HTTPStatus.MULTIPLE_CHOICES
                ),
            )
            return HttpAttempt(
                status_code=response.status,
                headers=response_headers,
                body=payload,
            )
        finally:
            self._release_manager()

    def _acquire_manager(self, url: str) -> _PoolManager:
        """Reserve one shared lazy pool for a concurrent request."""
        try:
            proxy_url = _environment_proxy(url)
            with self._transport_lock:
                if self._closed:
                    raise TransientError("HTTP client is closed.") from None
                manager, retired = self._manager(proxy_url)
                self._active_requests += 1
        except (
            urllib3.exceptions.HTTPError,
            ValueError,
            OSError,
        ):
            raise TransientError(
                "HTTP transport configuration failed."
            ) from None
        if retired is not None:
            retired.clear()
        return manager

    def _manager(
        self,
        proxy_url: str | None,
    ) -> tuple[_PoolManager, _PoolManager | None]:
        """Return one manager while the transport lock is held."""
        if proxy_url is None:
            if self._direct_manager is None:
                self._direct_manager = urllib3.PoolManager(
                    retries=False,
                    cert_reqs="CERT_REQUIRED",
                )
            return self._direct_manager, None
        retired: _PoolManager | None = None
        if self._proxy_manager is None or proxy_url != self._proxy_url:
            retired = self._proxy_manager
            self._proxy_manager = urllib3.ProxyManager(
                proxy_url,
                retries=False,
                cert_reqs="CERT_REQUIRED",
            )
            self._proxy_url = proxy_url
            if retired is not None and self._active_requests > 0:
                self._retired_managers.append(retired)
                retired = None
        return self._proxy_manager, retired

    def _release_manager(self) -> None:
        """Release one request and clear pools only after all users finish."""
        with self._transport_lock:
            self._active_requests -= 1
            if self._active_requests < 0:
                raise AssertionError("HTTP request ownership underflow.")
            managers = (
                self._detach_managers()
                if self._active_requests == 0
                else ()
            )
        _clear_managers(managers)

    def _detach_managers(self) -> tuple[_PoolManager, ...]:
        """Detach pools eligible for clearing while the lock is held."""
        managers = list(self._retired_managers)
        self._retired_managers.clear()
        if self._closed:
            if self._proxy_manager is not None:
                managers.append(self._proxy_manager)
                self._proxy_manager = None
                self._proxy_url = None
            if self._direct_manager is not None:
                managers.append(self._direct_manager)
                self._direct_manager = None
        return tuple(managers)


def _clear_managers(managers: tuple[_PoolManager, ...]) -> None:
    """Clear detached pools outside the transport-state lock."""
    for manager in managers:
        manager.clear()


def _require_https(url: str) -> None:
    """Reject non-HTTPS and hostless URLs before transport access."""
    invalid = True
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError:
        pass
    else:
        invalid = (
            parsed.scheme.lower() != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or (port is not None and not 0 < port < _MAX_PORT_EXCLUSIVE)
        )
    if invalid:
        raise InsecureUrlError from None


def _environment_proxy(url: str) -> str | None:
    """Resolve the native environment/system proxy for an HTTPS URL."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if host is None:
        return None
    port = parsed.port
    bypass_host = (
        f"[{host}]:{port}"
        if port is not None and ":" in host
        else f"{host}:{port}"
        if port is not None
        else host
    )
    if urllib.request.proxy_bypass(bypass_host):
        return None
    proxies = urllib.request.getproxies()
    return proxies.get("https") or proxies.get("all")


def _encode_json(payload: JsonObject) -> bytes:
    """Encode one bounded JSON request without non-standard numbers."""
    failed = False
    body = b""
    try:
        body = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except TypeError, ValueError, RecursionError:
        failed = True
    if failed:
        raise InvalidPayloadError from None
    _require_request_bound(body, JSON_REQUEST_LIMIT)
    return body


def _require_request_bound(body: bytes, limit: int) -> None:
    """Reject a request body that exceeds its fixed operation bound."""
    if len(body) > limit:
        raise InvalidPayloadError from None


def _read_bounded(
    response: urllib3.BaseHTTPResponse,
    limit: int,
    discard_oversized: bool,
) -> bytes:
    """Read at most one byte beyond a response's fixed body bound."""
    read_failure = False
    invalid_encoding = False
    payload = b""
    try:
        payload = response.read(limit + 1, decode_content=True)
    except urllib3.exceptions.DecodeError:
        invalid_encoding = True
    except urllib3.exceptions.HTTPError, OSError:
        read_failure = True
    if invalid_encoding or read_failure or len(payload) > limit:
        response.close()
        response.release_conn()
    else:
        # A bounded read shorter than ``limit + 1`` consumed EOF. urllib3
        # can safely return that live connection to its pool.
        response.release_conn()
    if invalid_encoding:
        if discard_oversized:
            return b""
        raise InvalidPayloadError from None
    if read_failure:
        if discard_oversized:
            return b""
        raise urllib3.exceptions.ProtocolError(
            "bounded response read failed"
        ) from None
    if len(payload) > limit:
        if discard_oversized:
            return b""
        raise InvalidPayloadError from None
    return payload


def _forbidden_error(payload: bytes) -> ForbiddenError:
    """Build a bounded, credential-safe forbidden diagnostic."""
    api_message: str | None = None
    required_scope: str | None = None
    try:
        decoded = decode_json_object(payload)
    except InvalidPayloadError:
        decoded = {}
    error_value = decoded.get("error")
    candidate = (
        error_value.get("message")
        if isinstance(error_value, dict)
        else decoded.get("message")
    )
    if isinstance(candidate, str):
        match = _SCOPE_REQUIREMENT_RE.search(candidate)
        if match is not None and match.group(1) in _SAFE_REQUIRED_SCOPES:
            required_scope = match.group(1)
            api_message = (
                f"OAuth token does not meet scope requirement {required_scope}"
            )
    summary = (
        f"HTTP 403 Forbidden: {api_message}"
        if api_message is not None
        else "HTTP 403 Forbidden."
    )
    return ForbiddenError(
        summary,
        api_message=api_message,
        required_scope=required_scope,
    )


def _require_operation(
    operation: HttpOperation,
    *allowed: HttpOperation,
) -> None:
    """Reject a caller-selected operation incompatible with a method."""
    if operation not in allowed:
        raise ValueError(
            f"Operation {operation.value!r} is invalid for this request."
        )

"""Load-bearing pooled HTTP facade tests."""

from collections.abc import Mapping, Sequence
from http import HTTPMethod, HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from urllib3.exceptions import ProtocolError
from urllib3.util import Timeout

from sidekick_usages.errors import (
    AuthError,
    ForbiddenError,
    HttpStatusError,
    InsecureUrlError,
    InvalidPayloadError,
)
from sidekick_usages.http.client import (
    DISCARD_BODY_LIMIT,
    FORM_REQUEST_LIMIT,
    JSON_REQUEST_LIMIT,
    JSON_RESPONSE_LIMIT,
    HttpClient,
)
from sidekick_usages.http.types import HttpOperation

type _Outcome = _Response | BaseException


class _Response:
    """Minimal streaming response with observable pool disposition."""

    def __init__(
        self,
        status: int,
        body: bytes = b"",
        headers: Mapping[str, str] | None = None,
        read_error: BaseException | None = None,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self._body = body
        self._read_error = read_error
        self.close_calls = 0
        self.release_calls = 0

    def read(self, amount: int, *, decode_content: bool) -> bytes:
        """Return the requested bounded prefix."""
        assert decode_content is True
        if self._read_error is not None:
            raise self._read_error
        return self._body[:amount]

    def close(self) -> None:
        """Record destructive response closure."""
        self.close_calls += 1

    def release_conn(self) -> None:
        """Record returning a reusable connection to the pool."""
        self.release_calls += 1


class _Manager:
    """Retry-disabled transport double with queued outcomes."""

    def __init__(self, outcomes: Sequence[_Outcome]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[HTTPMethod, bool, bool]] = []
        self.bodies: list[bytes | None] = []
        self.headers: list[dict[str, str]] = []
        self.clear_calls = 0

    def request(
        self,
        method: HTTPMethod,
        url: str,
        *,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: Timeout,
        preload_content: bool,
        decode_content: bool,
        redirect: bool,
        retries: bool,
    ) -> _Response:
        """Return or raise the next outcome while recording safety flags."""
        del url, timeout, preload_content, decode_content
        if not isinstance(method, HTTPMethod):
            raise AssertionError("request method is not an HTTPMethod")
        self.calls.append((method, redirect, retries))
        self.bodies.append(body)
        self.headers.append(dict(headers))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def clear(self) -> None:
        """Record deterministic pool closure."""
        self.clear_calls += 1


def _install_manager(
    monkeypatch: pytest.MonkeyPatch,
    client: HttpClient,
    manager: _Manager,
) -> None:
    """Route one constructed client through a test-owned transport."""
    monkeypatch.setattr(client, "_manager_for", lambda _url: manager)


def test_client_enforces_boundary_and_preserves_reusable_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful JSON is bounded, typed, retry-disabled, and reusable."""
    first = _Response(
        HTTPStatus.OK,
        b'{"ok":true}',
        {"X-Trace": "one"},
    )
    second = _Response(HTTPStatus.OK, b'{"ok":false}')
    responses = [first, second]
    manager = _Manager(responses)
    client = HttpClient()
    _install_manager(monkeypatch, client, manager)

    assert client.get_json("https://example.test/one", {}) == {"ok": True}
    assert client.get_json("https://example.test/two", {}) == {"ok": False}
    assert manager.calls == [
        (HTTPMethod.GET, False, False),
        (HTTPMethod.GET, False, False),
    ]
    assert [first.close_calls, second.close_calls] == [0, 0]
    assert [first.release_calls, second.release_calls] == [1, 1]


def test_local_server_connection_is_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complete bounded reads return a live connection to the real pool."""
    client_ports: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            client_ports.append(self.client_address[1])
            body = b'{"ok":true}'
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        "sidekick_usages.http.client._require_https",
        lambda _url: None,
    )
    monkeypatch.setattr(
        "sidekick_usages.http.client.urllib.request.proxy_bypass",
        lambda _host: True,
    )
    client = HttpClient()
    url = f"http://127.0.0.1:{server.server_port}/usage"
    try:
        assert client.get_json(url, {}) == {"ok": True}
        assert client.get_json(url, {}) == {"ok": True}
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join()

    first_port, second_port = client_ports
    assert first_port == second_port


def test_post_capabilities_encode_and_return_sidekick_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The three reviewed POST shapes preserve their concrete contracts."""
    header_response = _Response(
        HTTPStatus.OK,
        b"x" * (DISCARD_BODY_LIMIT + 1),
        {"X-Usage": "ready"},
    )
    manager = _Manager(
        [
            header_response,
            _Response(HTTPStatus.OK, b'{"access_token":"claude-new"}'),
            _Response(HTTPStatus.OK, b'{"access_token":"codex-new"}'),
        ]
    )
    client = HttpClient()
    _install_manager(monkeypatch, client, manager)

    returned_headers = client.post_capture_headers(
        "https://example.test/probe",
        {"prompt": "quota"},
        {"Authorization": "Bearer sentinel"},
        operation=HttpOperation.CLAUDE_PROBE,
    )
    claude = client.post_json(
        "https://example.test/claude-refresh",
        {"refresh_token": "claude old"},
        operation=HttpOperation.CLAUDE_REFRESH,
    )
    codex = client.post_form(
        "https://example.test/codex-refresh",
        {"refresh_token": "codex old"},
        operation=HttpOperation.CODEX_REFRESH,
    )

    assert returned_headers == {"x-usage": "ready"}
    assert claude == {"access_token": "claude-new"}
    assert codex == {"access_token": "codex-new"}
    assert manager.bodies == [
        b'{"prompt":"quota"}',
        b'{"refresh_token":"claude old"}',
        b"refresh_token=codex+old",
    ]
    assert [call[0] for call in manager.calls] == [
        HTTPMethod.POST,
        HTTPMethod.POST,
        HTTPMethod.POST,
    ]
    assert all(
        not redirect and not retries for _, redirect, retries in manager.calls
    )
    assert header_response.close_calls == 1


def test_request_and_response_size_bounds_fail_before_unbounded_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON and form writes plus JSON reads enforce their named bounds."""
    response = _Response(
        HTTPStatus.OK,
        b"x" * (JSON_RESPONSE_LIMIT + 1),
    )
    manager = _Manager([response])
    client = HttpClient()
    _install_manager(monkeypatch, client, manager)

    with pytest.raises(InvalidPayloadError):
        client.get_json("https://example.test/oversized", {})
    with pytest.raises(InvalidPayloadError):
        client.post_json(
            "https://example.test/oversized-json",
            {"value": "x" * JSON_REQUEST_LIMIT},
            operation=HttpOperation.CLAUDE_REFRESH,
        )
    with pytest.raises(InvalidPayloadError):
        client.post_form(
            "https://example.test/oversized-form",
            {"value": "x" * FORM_REQUEST_LIMIT},
            operation=HttpOperation.CODEX_REFRESH,
        )

    assert len(manager.calls) == 1
    assert response.close_calls == 1


@pytest.mark.parametrize(
    "body",
    [b"not-json", b"[]", b"null", b'"scalar"'],
)
def test_client_rejects_non_object_json(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    """Malformed and non-object success bodies share one typed failure."""
    client = HttpClient()
    _install_manager(monkeypatch, client, _Manager([_Response(200, body)]))

    with pytest.raises(InvalidPayloadError):
        client.get_json("https://example.test/data", {})


def test_non_https_is_rejected_before_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential-bearing URL cannot reach a non-HTTPS transport."""
    manager = _Manager([_Response(200, b"{}")])
    client = HttpClient()
    _install_manager(monkeypatch, client, manager)

    with pytest.raises(InsecureUrlError) as exc_info:
        client.get_json("http://secret.example/token?token=sentinel", {})

    assert manager.calls == []
    assert "sentinel" not in str(exc_info.value)

    with pytest.raises(InsecureUrlError) as invalid_port:
        client.get_json("https://example.test:not-a-port/token", {})
    assert invalid_port.value.__cause__ is None
    assert invalid_port.value.__context__ is None
    assert manager.calls == []


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (_Response(HTTPStatus.UNAUTHORIZED), AuthError),
        (_Response(HTTPStatus.NOT_FOUND), HttpStatusError),
    ],
)
def test_terminal_statuses_do_not_retry(
    monkeypatch: pytest.MonkeyPatch,
    response: _Response,
    error_type: type[Exception],
) -> None:
    """Authentication and permanent rejection statuses are terminal."""
    manager = _Manager([response])
    client = HttpClient()
    _install_manager(monkeypatch, client, manager)

    with pytest.raises(error_type):
        client.get_json("https://example.test/data", {})

    assert len(manager.calls) == 1


def test_auth_status_survives_broken_optional_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed diagnostic read cannot demote a terminal 401."""
    response = _Response(
        HTTPStatus.UNAUTHORIZED,
        read_error=ProtocolError("broken body"),
    )
    manager = _Manager([response])
    client = HttpClient()
    _install_manager(monkeypatch, client, manager)

    with pytest.raises(AuthError):
        client.get_json("https://example.test/data", {})

    assert len(manager.calls) == 1


def test_forbidden_exposes_only_normalized_scope_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 retains route guidance without echoing arbitrary body data."""
    secret = "credential-sentinel"
    body = (
        b'{"error":{"message":"OAuth token does not meet scope '
        b"requirement user:profile; " + secret.encode() + b'"}}'
    )
    client = HttpClient()
    _install_manager(
        monkeypatch,
        client,
        _Manager([_Response(HTTPStatus.FORBIDDEN, body)]),
    )

    with pytest.raises(ForbiddenError) as exc_info:
        client.get_json("https://example.test/data", {})

    assert exc_info.value.required_scope == "user:profile"
    assert exc_info.value.api_message == (
        "OAuth token does not meet scope requirement user:profile"
    )
    assert secret not in str(exc_info.value)

    reflected = _Response(
        HTTPStatus.FORBIDDEN,
        b'{"error":{"message":"scope requirement sk-secret-credential"}}',
    )
    _install_manager(monkeypatch, client, _Manager([reflected]))
    with pytest.raises(ForbiddenError) as reflected_info:
        client.get_json("https://example.test/data", {})
    assert reflected_info.value.required_scope is None
    assert "sk-secret-credential" not in str(reflected_info.value)


def test_pool_is_lazy_and_close_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Port-qualified NO_PROXY keeps one lazy direct pool reusable."""
    manager = _Manager([_Response(200, b"{}")])
    constructions = 0

    def build_pool(**_configuration: object) -> _Manager:
        nonlocal constructions
        constructions += 1
        return manager

    monkeypatch.setattr(
        "sidekick_usages.http.client.urllib3.PoolManager",
        build_pool,
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@proxy.test:8080")
    monkeypatch.setenv("https_proxy", "http://user:secret@proxy.test:8080")
    monkeypatch.setenv("NO_PROXY", "example.test:8443")
    monkeypatch.setenv("no_proxy", "example.test:8443")
    client = HttpClient()
    assert constructions == 0

    assert client.get_json("https://example.test:8443/data", {}) == {}
    client.close()
    client.close()

    assert constructions == 1
    assert manager.clear_calls == 1

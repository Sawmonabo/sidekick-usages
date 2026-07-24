"""Load-bearing tests for the closed HTTP retry contract."""

import random
from datetime import timedelta
from email.utils import format_datetime

import pytest
from urllib3.exceptions import ConnectTimeoutError, HTTPError, ProtocolError

from sidekick_usages.errors import RateLimitError, TransientError
from sidekick_usages.http.models import HttpAttempt
from sidekick_usages.http.retry import (
    RETRY_AFTER_CAP_SECONDS,
    RetryExecutor,
    parse_retry_after,
)
from sidekick_usages.http.types import HttpOperation
from tests.test_support import REFERENCE_TIME, FixedClock

RETRY_AFTER_SECONDS = 7


@pytest.mark.parametrize(
    ("operation", "status", "headers", "attempts", "error_type"),
    [
        (HttpOperation.SAFE_READ, 500, {}, 3, TransientError),
        (HttpOperation.SAFE_READ, 502, {}, 3, TransientError),
        (HttpOperation.SAFE_READ, 503, {}, 3, TransientError),
        (HttpOperation.SAFE_READ, 504, {}, 3, TransientError),
        (HttpOperation.SAFE_READ, 529, {}, 3, TransientError),
        (HttpOperation.SAFE_READ, 501, {}, 1, TransientError),
        (
            HttpOperation.SAFE_READ,
            503,
            {"x-should-retry": "false"},
            1,
            TransientError,
        ),
        (HttpOperation.CLAUDE_PROBE, 529, {}, 3, TransientError),
        (HttpOperation.CLAUDE_PROBE, 429, {}, 3, RateLimitError),
        (HttpOperation.CLAUDE_REFRESH, 429, {}, 1, RateLimitError),
        (HttpOperation.CODEX_REFRESH, 429, {}, 1, RateLimitError),
        (HttpOperation.CLAUDE_HEARTBEAT, 429, {}, 3, RateLimitError),
        (HttpOperation.CODEX_HEARTBEAT, 429, {}, 1, RateLimitError),
    ],
)
def test_closed_operation_status_matrix(
    operation: HttpOperation,
    status: int,
    headers: dict[str, str],
    attempts: int,
    error_type: type[Exception],
) -> None:
    """Only reviewed operations retry authoritative status failures."""
    calls = 0

    def attempt(_remaining: float) -> HttpAttempt:
        nonlocal calls
        calls += 1
        return HttpAttempt(status, headers, b"")

    executor = RetryExecutor(
        clock=FixedClock(),
        sleep=lambda _delay: None,
        random_source=random.Random(0),
    )
    with pytest.raises(error_type):
        executor.execute(operation, attempt)
    assert calls == attempts


@pytest.mark.parametrize(
    ("operation", "failure", "attempts"),
    [
        (
            HttpOperation.CLAUDE_REFRESH,
            ConnectTimeoutError(None, "connect"),
            3,
        ),
        (
            HttpOperation.SAFE_READ,
            ProtocolError("ambiguous read"),
            3,
        ),
        (
            HttpOperation.CLAUDE_REFRESH,
            ProtocolError("ambiguous send"),
            1,
        ),
    ],
)
def test_closed_operation_transport_matrix(
    operation: HttpOperation,
    failure: HTTPError,
    attempts: int,
) -> None:
    """POST retries require proof that the request was never sent."""
    calls = 0

    def attempt(_remaining: float) -> HttpAttempt:
        nonlocal calls
        calls += 1
        raise failure

    executor = RetryExecutor(
        clock=FixedClock(),
        sleep=lambda _delay: None,
        random_source=random.Random(0),
    )
    with pytest.raises(TransientError):
        executor.execute(operation, attempt)
    assert calls == attempts


def test_retry_after_parsing_is_bounded_and_standards_compliant() -> None:
    """Delay seconds and HTTP dates select integral capped guidance."""
    future = format_datetime(REFERENCE_TIME + timedelta(seconds=2.5))
    past = format_datetime(REFERENCE_TIME - timedelta(seconds=1))
    enormous = "9" * 10_000
    unrepresentable = "Fri, 31 Dec 9999 23:59:59 -2359"
    future_delay_seconds = 3
    date_clock_calls = 3
    clock = FixedClock()

    assert parse_retry_after(str(RETRY_AFTER_SECONDS), clock) == (
        RETRY_AFTER_SECONDS
    )
    assert parse_retry_after(None, clock) is None
    assert parse_retry_after("later", clock) is None
    assert parse_retry_after(enormous, clock) == RETRY_AFTER_CAP_SECONDS
    assert clock.calls == 0
    assert parse_retry_after(future, clock) == future_delay_seconds
    assert parse_retry_after(past, clock) == 0
    assert parse_retry_after(unrepresentable, clock) is None
    assert clock.calls == date_clock_calls


def test_elapsed_deadline_stops_despite_wall_movement_or_suspension() -> None:
    """Monotonic expiry stops work despite suspension or wall movement."""
    elapsed = 0.0
    attempts = 0
    wall_clock = FixedClock()
    retry_date = format_datetime(
        REFERENCE_TIME + timedelta(seconds=RETRY_AFTER_SECONDS)
    )

    def monotonic() -> float:
        return elapsed

    def oversleep(_delay: float) -> None:
        nonlocal elapsed
        elapsed = 16.0
        wall_clock.value = REFERENCE_TIME - timedelta(days=365)

    def rate_limited(_remaining: float) -> HttpAttempt:
        nonlocal attempts
        attempts += 1
        return HttpAttempt(429, {"retry-after": retry_date}, b"")

    executor = RetryExecutor(
        clock=wall_clock,
        monotonic=monotonic,
        sleep=oversleep,
        random_source=random.Random(0),
    )
    with pytest.raises(RateLimitError) as exc_info:
        executor.execute(HttpOperation.SAFE_READ, rate_limited)
    assert attempts == 1
    assert exc_info.value.retry_after == RETRY_AFTER_SECONDS
    assert wall_clock.calls == 1

    suspended_samples = iter((0.0, 20.0))
    suspended_attempts = 0

    def suspended_attempt(_remaining: float) -> HttpAttempt:
        nonlocal suspended_attempts
        suspended_attempts += 1
        return HttpAttempt(200, {}, b"{}")

    suspended_executor = RetryExecutor(
        clock=FixedClock(),
        monotonic=lambda: next(suspended_samples),
    )
    with pytest.raises(TransientError):
        suspended_executor.execute(
            HttpOperation.SAFE_READ,
            suspended_attempt,
        )
    assert suspended_attempts == 0


def test_delay_beyond_deadline_is_retained_without_sleeping() -> None:
    """A full server wait must fit before another attempt can begin."""
    long_wait_seconds = 16
    calls = 0
    sleeps: list[float] = []

    def rate_limited(_remaining: float) -> HttpAttempt:
        nonlocal calls
        calls += 1
        return HttpAttempt(
            429,
            {"retry-after": str(long_wait_seconds)},
            b"",
        )

    executor = RetryExecutor(clock=FixedClock(), sleep=sleeps.append)
    with pytest.raises(RateLimitError) as exc_info:
        executor.execute(HttpOperation.SAFE_READ, rate_limited)
    assert calls == 1
    assert exc_info.value.retry_after == long_wait_seconds
    assert sleeps == []


def test_last_valid_guidance_survives_malformed_successors() -> None:
    """A later malformed header cannot erase valid 429 guidance."""
    results = iter(
        (
            HttpAttempt(429, {"retry-after": "1"}, b""),
            HttpAttempt(429, {"retry-after": "bad"}, b""),
            HttpAttempt(429, {"retry-after": "bad"}, b""),
        )
    )
    executor = RetryExecutor(
        clock=FixedClock(),
        sleep=lambda _delay: None,
        random_source=random.Random(0),
    )
    with pytest.raises(RateLimitError) as exc_info:
        executor.execute(
            HttpOperation.SAFE_READ,
            lambda _remaining: next(results),
        )
    assert exc_info.value.retry_after == 1


def test_retry_after_guides_selected_server_retries() -> None:
    """A valid server-directed delay controls each eligible 503 retry."""
    sleeps: list[float] = []
    executor = RetryExecutor(
        clock=FixedClock(),
        sleep=sleeps.append,
        random_source=random.Random(0),
    )
    with pytest.raises(TransientError):
        executor.execute(
            HttpOperation.SAFE_READ,
            lambda _remaining: HttpAttempt(
                503,
                {"retry-after": "2"},
                b"",
            ),
        )
    assert sleeps == [2.0, 2.0]


def test_transport_errors_are_redacted_and_drop_library_context() -> None:
    """Third-party exceptions and credential text stop at the facade."""
    secret = "authorization-sentinel"
    executor = RetryExecutor(clock=FixedClock())

    def attempt(_remaining: float) -> HttpAttempt:
        raise ProtocolError(f"failed with {secret}")

    with pytest.raises(TransientError) as exc_info:
        executor.execute(HttpOperation.CLAUDE_REFRESH, attempt)
    assert secret not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None

"""Closed retry policies for Sidekick HTTP operations."""

import math
import random
import re
import time
from collections.abc import Callable
from datetime import UTC
from email.utils import parsedate_to_datetime
from http import HTTPStatus
from typing import NoReturn

import urllib3.exceptions

from sidekick_usages.clock import Clock
from sidekick_usages.errors import RateLimitError, TransientError
from sidekick_usages.http.models import (
    HttpAttempt,
    RetryPolicy,
    TerminalState,
)
from sidekick_usages.http.types import (
    HttpOperation,
    TerminalOutcome,
    TransportFailure,
)

TOTAL_ATTEMPTS = 3
OPERATION_BUDGET_SECONDS = 15.0
CONNECT_TIMEOUT_SECONDS = 3.0
READ_TIMEOUT_SECONDS = 10.0
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 8.0
RETRY_AFTER_CAP_SECONDS = 21_600

_RETRYABLE_SERVER_STATUSES = frozenset(
    {
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.BAD_GATEWAY,
        HTTPStatus.SERVICE_UNAVAILABLE,
        HTTPStatus.GATEWAY_TIMEOUT,
        529,
    }
)
_DELAY_SECONDS_RE = re.compile(r"[0-9]+")
_SERVER_ERROR_START = 500
_SERVER_ERROR_END = 600
_POLICIES = {
    HttpOperation.SAFE_READ: RetryPolicy(
        ambiguous_transport=True,
        rate_limit=True,
        server_status=True,
    ),
    HttpOperation.CLAUDE_PROBE: RetryPolicy(
        ambiguous_transport=False,
        rate_limit=True,
        server_status=True,
    ),
    HttpOperation.CLAUDE_REFRESH: RetryPolicy(
        ambiguous_transport=False,
        rate_limit=False,
        server_status=False,
    ),
    HttpOperation.CLAUDE_HEARTBEAT: RetryPolicy(
        ambiguous_transport=False,
        rate_limit=True,
        server_status=False,
    ),
    HttpOperation.CODEX_HEARTBEAT: RetryPolicy(
        ambiguous_transport=False,
        rate_limit=False,
        server_status=False,
    ),
}


class RetryExecutor:
    """Execute requests under the fixed Sidekick retry contract."""

    def __init__(
        self,
        *,
        clock: Clock,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
    ) -> None:
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._random = random_source or random.SystemRandom()

    def execute(
        self,
        operation: HttpOperation,
        attempt: Callable[[float], HttpAttempt],
    ) -> HttpAttempt:
        """Run one HTTP operation within attempt and elapsed bounds.

        :param operation: Reviewed operation-safety class.
        :param attempt: Internal single-attempt transport function.
        :returns: The bounded terminal response.
        :raises RateLimitError: If a 429 cannot be retried.
        :raises TransientError: If transport or server retries stop.
        """
        policy = _POLICIES[operation]
        started_at = self._monotonic()
        attempt_number = 0
        terminal = TerminalState(TerminalOutcome.TRANSPORT)
        last_valid_retry_after: int | None = None

        while attempt_number < TOTAL_ATTEMPTS:
            remaining = self._remaining(started_at)
            if remaining <= 0:
                break
            attempt_number += 1
            result, transport_failure = _run_attempt(attempt, remaining)
            if result is not None:
                decision = self._response_decision(
                    result,
                    policy,
                    attempt_number,
                    started_at,
                )
                if decision is None:
                    return result
                outcome, delay, retry_after = decision
                if retry_after is not None:
                    last_valid_retry_after = retry_after
                terminal = TerminalState(
                    outcome,
                    last_valid_retry_after,
                )
                if delay is None:
                    _raise_terminal(
                        terminal,
                        attempt_number,
                    )
                self._sleep(delay)
                continue

            if transport_failure is None:
                raise AssertionError("missing transport failure")
            terminal = TerminalState(TerminalOutcome.TRANSPORT)
            delay = self._transport_delay(
                transport_failure,
                policy,
                attempt_number,
                started_at,
            )
            if delay is None:
                _raise_terminal(
                    terminal,
                    attempt_number,
                )
            self._sleep(delay)

        return _raise_terminal(
            terminal,
            attempt_number,
        )

    def _response_decision(
        self,
        result: HttpAttempt,
        policy: RetryPolicy,
        attempt_number: int,
        started_at: float,
    ) -> tuple[TerminalOutcome, float | None, int | None] | None:
        """Return a terminal or retry decision for one response."""
        status = result.status_code
        if status == HTTPStatus.TOO_MANY_REQUESTS:
            retry_after = parse_retry_after(
                result.headers.get("retry-after"),
                self._clock,
            )
            may_retry = policy.rate_limit and not _rejects_retry(result)
            delay = (
                self._retry_delay(
                    attempt_number,
                    started_at,
                    retry_after,
                )
                if may_retry
                else None
            )
            return TerminalOutcome.RATE_LIMIT, delay, retry_after

        if _SERVER_ERROR_START <= status < _SERVER_ERROR_END:
            retry_after = parse_retry_after(
                result.headers.get("retry-after"),
                self._clock,
            )
            may_retry = (
                policy.server_status
                and status in _RETRYABLE_SERVER_STATUSES
                and not _rejects_retry(result)
            )
            delay = (
                self._retry_delay(
                    attempt_number,
                    started_at,
                    retry_after,
                )
                if may_retry
                else None
            )
            return TerminalOutcome.SERVER, delay, retry_after
        return None

    def _transport_delay(
        self,
        failure: TransportFailure,
        policy: RetryPolicy,
        attempt_number: int,
        started_at: float,
    ) -> float | None:
        """Return a safe retry delay for a transport failure."""
        may_retry = failure is TransportFailure.PROVEN_CONNECT or (
            failure is TransportFailure.AMBIGUOUS
            and policy.ambiguous_transport
        )
        if not may_retry:
            return None
        return self._retry_delay(attempt_number, started_at, None)

    def _retry_delay(
        self,
        attempt_number: int,
        started_at: float,
        retry_after: int | None,
    ) -> float | None:
        """Select a bounded delay if another attempt can still begin."""
        if attempt_number >= TOTAL_ATTEMPTS:
            return None
        if retry_after is None:
            upper = min(
                BACKOFF_CAP_SECONDS,
                BACKOFF_BASE_SECONDS * (2 ** (attempt_number - 1)),
            )
            delay = self._random.uniform(0.0, upper)
        else:
            delay = float(retry_after)
        remaining = self._remaining(started_at)
        if delay >= remaining:
            return None
        return delay

    def _remaining(self, started_at: float) -> float:
        """Return the non-negative remaining monotonic budget."""
        elapsed = max(0.0, self._monotonic() - started_at)
        return max(0.0, OPERATION_BUDGET_SECONDS - elapsed)


def parse_retry_after(raw: str | None, clock: Clock) -> int | None:
    """Parse and cap an RFC 9110 ``Retry-After`` value.

    :param raw: Header value, if supplied.
    :param clock: Wall clock sampled only for an absolute HTTP date.
    :returns: Whole seconds selected for user guidance and retry.
    """
    if raw is None:
        return None
    value = raw.strip()
    if _DELAY_SECONDS_RE.fullmatch(value):
        return _parse_delay_seconds(value)
    return _parse_http_date(value, clock)


def _parse_delay_seconds(value: str) -> int:
    """Parse delay seconds without converting an unbounded integer."""
    normalized = value.lstrip("0") or "0"
    cap = str(RETRY_AFTER_CAP_SECONDS)
    if len(normalized) > len(cap) or (
        len(normalized) == len(cap) and normalized > cap
    ):
        return RETRY_AFTER_CAP_SECONDS
    return int(normalized)


def _parse_http_date(value: str, clock: Clock) -> int | None:
    """Parse an absolute date with a lazily acquired wall time."""
    try:
        retry_at = parsedate_to_datetime(value)
    except TypeError, ValueError, OverflowError:
        return None
    wall_time = clock.now()
    if retry_at.tzinfo is None or wall_time.tzinfo is None:
        return None
    try:
        delta = retry_at.astimezone(UTC) - wall_time.astimezone(UTC)
    except OverflowError, ValueError:
        return None
    seconds = max(0, math.ceil(delta.total_seconds()))
    return min(seconds, RETRY_AFTER_CAP_SECONDS)


def _run_attempt(
    attempt: Callable[[float], HttpAttempt],
    remaining: float,
) -> tuple[HttpAttempt | None, TransportFailure | None]:
    """Run one attempt and retain only a safe failure category."""
    try:
        return attempt(remaining), None
    except urllib3.exceptions.HTTPError as error:
        failure = _classify_transport_failure(error)
    except OSError:
        failure = TransportFailure.AMBIGUOUS
    return None, failure


def _rejects_retry(result: HttpAttempt) -> bool:
    """Return whether the server explicitly made retry terminal."""
    return result.headers.get("x-should-retry", "").strip().lower() == "false"


def _classify_transport_failure(
    error: urllib3.exceptions.HTTPError,
) -> TransportFailure:
    """Classify a urllib3 failure without exporting its details."""
    if isinstance(error, urllib3.exceptions.MaxRetryError):
        reason = error.reason
        if isinstance(reason, urllib3.exceptions.HTTPError):
            return _classify_transport_failure(reason)
    if isinstance(error, urllib3.exceptions.ProxyError):
        original = error.original_error
        if isinstance(original, urllib3.exceptions.ConnectTimeoutError):
            return TransportFailure.PROVEN_CONNECT
        return TransportFailure.AMBIGUOUS
    if isinstance(error, urllib3.exceptions.ConnectTimeoutError):
        return TransportFailure.PROVEN_CONNECT
    if isinstance(
        error,
        (
            urllib3.exceptions.ReadTimeoutError,
            urllib3.exceptions.ProtocolError,
            urllib3.exceptions.IncompleteRead,
            urllib3.exceptions.InvalidChunkLength,
        ),
    ):
        return TransportFailure.AMBIGUOUS
    return TransportFailure.TERMINAL


def _raise_terminal(
    terminal: TerminalState,
    attempts: int,
) -> NoReturn:
    """Raise one credential-safe Sidekick terminal error."""
    if terminal.outcome is TerminalOutcome.RATE_LIMIT:
        raise RateLimitError(
            f"Rate limited (HTTP 429) after {attempts} attempts.",
            retry_after=terminal.retry_after,
        ) from None
    if terminal.outcome is TerminalOutcome.SERVER:
        raise TransientError(
            f"Provider server failure after {attempts} attempts."
        ) from None
    raise TransientError(
        f"Network operation failed after {attempts} attempts."
    ) from None

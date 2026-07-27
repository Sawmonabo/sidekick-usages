"""HTTP retry boundary models."""

from dataclasses import dataclass

from sidekick_usages.http.types import TerminalOutcome


@dataclass(frozen=True, slots=True)
class HttpAttempt:
    """Bounded result of one transport attempt."""

    status_code: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class HttpHeaderResponse:
    """Bounded status and headers from one provider probe."""

    status_code: int
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry permissions for one closed operation class."""

    ambiguous_transport: bool
    rate_limit: bool
    server_status: bool
    capture_rate_limit: bool = False


@dataclass(frozen=True, slots=True)
class TerminalState:
    """Last typed outcome available if the elapsed budget expires."""

    outcome: TerminalOutcome
    retry_after: int | None = None

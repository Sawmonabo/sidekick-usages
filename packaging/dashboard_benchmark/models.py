"""Typed dashboard benchmark process protocol and measurements."""

from dataclasses import dataclass

from dashboard_benchmark.errors import DashboardBenchmarkError

FIRST_PAINT_PREFIX = "FIRST_PAINT"
TRACE_PREFIX = "TRACE"
FIELD_SEPARATOR = "|"
ORDINAL_SEPARATOR = ","
FIRST_PAINT_FIELD_COUNT = 3
TRACE_FIELD_COUNT = 10
TRUE_FIELD = "1"
FALSE_FIELD = "0"


def _decode_int(value: str, field: str) -> int:
    try:
        decoded = int(value)
    except ValueError:
        raise DashboardBenchmarkError(
            f"Dashboard benchmark {field} is not an integer."
        ) from None
    if decoded < 0:
        raise DashboardBenchmarkError(
            f"Dashboard benchmark {field} cannot be negative."
        )
    return decoded


def _decode_ordinals(value: str, field: str) -> tuple[int, ...]:
    if not value:
        return ()
    return tuple(
        _decode_int(item, field) for item in value.split(ORDINAL_SEPARATOR)
    )


def _encode_ordinals(values: tuple[int, ...]) -> str:
    return ORDINAL_SEPARATOR.join(str(value) for value in values)


def _decode_bool(value: str, field: str) -> bool:
    if value == TRUE_FIELD:
        return True
    if value == FALSE_FIELD:
        return False
    raise DashboardBenchmarkError(
        f"Dashboard benchmark {field} is not a boolean."
    )


def _encode_bool(value: bool) -> str:
    return TRUE_FIELD if value else FALSE_FIELD


@dataclass(frozen=True, slots=True, kw_only=True)
class FirstPaintSignal:
    """One completed cached-dashboard first paint."""

    account_count: int
    rendered_bytes: int

    def __post_init__(self) -> None:
        if self.account_count <= 0 or self.rendered_bytes <= 0:
            raise DashboardBenchmarkError(
                "First paint requires accounts and rendered output."
            )

    def encode(self) -> str:
        """Encode the bounded parent-child first-paint signal."""
        return FIELD_SEPARATOR.join(
            (
                FIRST_PAINT_PREFIX,
                str(self.account_count),
                str(self.rendered_bytes),
            )
        )

    @classmethod
    def decode(cls, line: str) -> FirstPaintSignal:
        """Decode one exact first-paint signal."""
        fields = line.rstrip("\n").split(FIELD_SEPARATOR)
        if (
            len(fields) != FIRST_PAINT_FIELD_COUNT
            or fields[0] != FIRST_PAINT_PREFIX
        ):
            raise DashboardBenchmarkError(
                "Dashboard benchmark first-paint signal is malformed."
            )
        return cls(
            account_count=_decode_int(fields[1], "account count"),
            rendered_bytes=_decode_int(fields[2], "rendered bytes"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ChildTrace:
    """One bounded cursor and concurrent-lookup trace."""

    process_id: int
    reference_cursor_p95_ns: int
    expanded_cursor_p95_ns: int
    submitted_ordinals: tuple[int, ...]
    completion_ordinals: tuple[int, ...]
    final_ordinals: tuple[int, ...]
    slow_ordinal: int
    reaped_lookup_worker_ru_maxrss_bytes: int
    lookup_worker_reaped: bool

    def __post_init__(self) -> None:
        if (
            self.reference_cursor_p95_ns <= 0
            or self.expanded_cursor_p95_ns <= 0
            or self.process_id <= 0
            or not self.submitted_ordinals
            or not self.completion_ordinals
            or not self.final_ordinals
            or self.reaped_lookup_worker_ru_maxrss_bytes <= 0
        ):
            raise DashboardBenchmarkError(
                "Dashboard benchmark child trace is incomplete."
            )

    def encode(self) -> str:
        """Encode one exact bounded child trace."""
        return FIELD_SEPARATOR.join(
            (
                TRACE_PREFIX,
                str(self.process_id),
                str(self.reference_cursor_p95_ns),
                str(self.expanded_cursor_p95_ns),
                _encode_ordinals(self.submitted_ordinals),
                _encode_ordinals(self.completion_ordinals),
                _encode_ordinals(self.final_ordinals),
                str(self.slow_ordinal),
                str(self.reaped_lookup_worker_ru_maxrss_bytes),
                _encode_bool(self.lookup_worker_reaped),
            )
        )

    @classmethod
    def decode(cls, line: str) -> ChildTrace:
        """Decode one exact bounded child trace."""
        fields = line.rstrip("\n").split(FIELD_SEPARATOR)
        if len(fields) != TRACE_FIELD_COUNT or fields[0] != TRACE_PREFIX:
            raise DashboardBenchmarkError(
                "Dashboard benchmark child trace is malformed."
            )
        return cls(
            process_id=_decode_int(fields[1], "process id"),
            reference_cursor_p95_ns=_decode_int(
                fields[2],
                "reference cursor p95",
            ),
            expanded_cursor_p95_ns=_decode_int(
                fields[3],
                "expanded cursor p95",
            ),
            submitted_ordinals=_decode_ordinals(
                fields[4],
                "submitted ordinal",
            ),
            completion_ordinals=_decode_ordinals(
                fields[5],
                "completion ordinal",
            ),
            final_ordinals=_decode_ordinals(
                fields[6],
                "final ordinal",
            ),
            slow_ordinal=_decode_int(fields[7], "slow ordinal"),
            reaped_lookup_worker_ru_maxrss_bytes=_decode_int(
                fields[8],
                "lookup worker RSS",
            ),
            lookup_worker_reaped=_decode_bool(
                fields[9],
                "lookup worker reaped",
            ),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class BenchmarkResult:
    """One release measurement from the reaped dashboard trace process."""

    first_paint_ms: float
    reference_cursor_p95_ms: float
    expanded_cursor_p95_ms: float
    reaped_trace_process_ru_maxrss_mib: float
    trace_process_reaped: bool
    reaped_lookup_worker_ru_maxrss_mib: float
    lookup_worker_reaped: bool
    trace: ChildTrace

    def __post_init__(self) -> None:
        if (
            self.first_paint_ms <= 0
            or self.reference_cursor_p95_ms <= 0
            or self.expanded_cursor_p95_ms <= 0
            or self.reaped_trace_process_ru_maxrss_mib <= 0
            or self.reaped_lookup_worker_ru_maxrss_mib <= 0
        ):
            raise DashboardBenchmarkError(
                "Dashboard benchmark measurements must be positive."
            )

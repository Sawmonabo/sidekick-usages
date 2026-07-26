"""Typed dashboard benchmark process protocol and measurements."""

from dataclasses import dataclass
from enum import StrEnum

from dashboard_benchmark.errors import DashboardBenchmarkError
from sidekick_usages.core.types import ProviderId

FIRST_PAINT_PREFIX = "FIRST_PAINT"
TRACE_PREFIX = "TRACE"
FIELD_SEPARATOR = "|"
ORDINAL_SEPARATOR = ","
TASK_SEPARATOR = ";"
TASK_FIELD_SEPARATOR = ","
FIRST_PAINT_FIELD_COUNT = 3
TRACE_FIELD_COUNT = 11
TASK_FIELD_COUNT = 4
TRUE_FIELD = "1"
FALSE_FIELD = "0"

type LookupTaskKey = int | ProviderId


class LookupTaskKind(StrEnum):
    """Closed task kinds admitted to the concurrent lookup trace."""

    ACCOUNT = "account"
    LOCAL_ACTIVITY = "local_activity"


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


@dataclass(frozen=True, slots=True)
class LookupTaskIdentity:
    """One typed account or provider-local lookup identity."""

    kind: LookupTaskKind
    key: LookupTaskKey

    def __post_init__(self) -> None:
        account_identity = (
            self.kind is LookupTaskKind.ACCOUNT
            and type(self.key) is int
            and self.key >= 0
        )
        local_identity = (
            self.kind is LookupTaskKind.LOCAL_ACTIVITY
            and isinstance(self.key, ProviderId)
        )
        if not account_identity and not local_identity:
            raise DashboardBenchmarkError(
                "Dashboard benchmark task identity is invalid."
            )

    @classmethod
    def account(cls, ordinal: int) -> LookupTaskIdentity:
        """Return one saved-account task identity."""
        return cls(LookupTaskKind.ACCOUNT, ordinal)

    @classmethod
    def local_activity(
        cls,
        provider_id: ProviderId,
    ) -> LookupTaskIdentity:
        """Return one provider-local activity task identity."""
        return cls(LookupTaskKind.LOCAL_ACTIVITY, provider_id)

    def encode(self) -> str:
        """Encode the typed identity without ambiguous field content."""
        return TASK_FIELD_SEPARATOR.join((self.kind.value, str(self.key)))


@dataclass(frozen=True, slots=True)
class LookupTaskStart:
    """Process and native-thread evidence for one started lookup task."""

    identity: LookupTaskIdentity
    process_id: int
    thread_id: int

    def __post_init__(self) -> None:
        if self.process_id <= 0 or self.thread_id <= 0:
            raise DashboardBenchmarkError(
                "Dashboard benchmark task execution identity is invalid."
            )

    def encode(self) -> str:
        """Encode one complete task-start record."""
        return TASK_FIELD_SEPARATOR.join(
            (
                self.identity.encode(),
                str(self.process_id),
                str(self.thread_id),
            )
        )

    @classmethod
    def decode(cls, value: str) -> LookupTaskStart:
        """Decode one strict task-start record."""
        fields = value.split(TASK_FIELD_SEPARATOR)
        if len(fields) != TASK_FIELD_COUNT:
            raise DashboardBenchmarkError(
                "Dashboard benchmark task-start trace is malformed."
            )
        try:
            kind = LookupTaskKind(fields[0])
        except ValueError:
            raise DashboardBenchmarkError(
                "Dashboard benchmark task kind is invalid."
            ) from None
        if kind is LookupTaskKind.ACCOUNT:
            identity = LookupTaskIdentity.account(
                _decode_int(fields[1], "account task identity")
            )
        else:
            try:
                provider_id = ProviderId(fields[1])
            except ValueError:
                raise DashboardBenchmarkError(
                    "Dashboard benchmark local provider is invalid."
                ) from None
            identity = LookupTaskIdentity.local_activity(provider_id)
        return cls(
            identity,
            _decode_int(fields[2], "task process id"),
            _decode_int(fields[3], "task thread id"),
        )


def _decode_task_starts(value: str) -> tuple[LookupTaskStart, ...]:
    if not value:
        return ()
    return tuple(
        LookupTaskStart.decode(item) for item in value.split(TASK_SEPARATOR)
    )


def _encode_task_starts(values: tuple[LookupTaskStart, ...]) -> str:
    return TASK_SEPARATOR.join(value.encode() for value in values)


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
    task_starts: tuple[LookupTaskStart, ...]
    completion_ordinals: tuple[int, ...]
    slow_ordinal: int
    thread_wave_process_free: bool
    lookup_worker_launch_count: int
    reaped_lookup_worker_ru_maxrss_bytes: int
    lookup_worker_reaped: bool

    def __post_init__(self) -> None:
        if (
            self.reference_cursor_p95_ns <= 0
            or self.expanded_cursor_p95_ns <= 0
            or self.process_id <= 0
            or not self.task_starts
            or not self.completion_ordinals
            or self.lookup_worker_launch_count <= 0
            or self.reaped_lookup_worker_ru_maxrss_bytes <= 0
        ):
            raise DashboardBenchmarkError(
                "Dashboard benchmark child trace is incomplete."
            )
        identities = tuple(start.identity for start in self.task_starts)
        if len(identities) != len(set(identities)):
            raise DashboardBenchmarkError(
                "Dashboard benchmark started one task more than once."
            )

    def encode(self) -> str:
        """Encode one exact bounded child trace."""
        return FIELD_SEPARATOR.join(
            (
                TRACE_PREFIX,
                str(self.process_id),
                str(self.reference_cursor_p95_ns),
                str(self.expanded_cursor_p95_ns),
                _encode_task_starts(self.task_starts),
                _encode_ordinals(self.completion_ordinals),
                str(self.slow_ordinal),
                _encode_bool(self.thread_wave_process_free),
                str(self.lookup_worker_launch_count),
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
            task_starts=_decode_task_starts(fields[4]),
            completion_ordinals=_decode_ordinals(
                fields[5],
                "completion ordinal",
            ),
            slow_ordinal=_decode_int(fields[6], "slow ordinal"),
            thread_wave_process_free=_decode_bool(
                fields[7],
                "thread wave process free",
            ),
            lookup_worker_launch_count=_decode_int(
                fields[8],
                "lookup worker launch count",
            ),
            reaped_lookup_worker_ru_maxrss_bytes=_decode_int(
                fields[9],
                "lookup worker RSS",
            ),
            lookup_worker_reaped=_decode_bool(
                fields[10],
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

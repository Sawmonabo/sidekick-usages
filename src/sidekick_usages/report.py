"""Provider-agnostic usage report types.

The CLI never touches provider-specific JSON. Each provider parses
its own response and returns a ``UsageReport`` made of ``UsageWindow``
entries. The renderer only knows about these abstractions.
"""

from dataclasses import dataclass, field


@dataclass
class UsageWindow:
    """One utilization bucket from a provider.

    :ivar name: Short display label (``"5h"``, ``"7d"``,
        ``"7d Opus"``).
    :ivar utilization: Percent in the range 0-100.
    :ivar resets_at: ISO-8601 timestamp string or ``None``.
    """

    name: str
    utilization: float
    resets_at: str | None

    @property
    def is_active(self) -> bool:
        """Whether this bucket has any meaningful data.

        A bucket is considered inactive when its utilization is 0
        AND no reset timestamp is set — meaning no cycle has opened
        for that bucket on this plan.

        :return: True if the bucket should be rendered.
        """
        return not (self.utilization == 0 and self.resets_at is None)


@dataclass
class UsageReport:
    """Parsed usage data from a provider.

    :ivar windows: Per-window utilization data.
    :ivar plan: Plan tag reported by the provider, if any.
    :ivar raw: Original JSON payload for debugging.
    """

    windows: list[UsageWindow] = field(default_factory=list)
    plan: str | None = None
    raw: dict[str, object] = field(default_factory=dict)

    def active_windows(self) -> list[UsageWindow]:
        """:return: Only windows that pass ``is_active``."""
        return [w for w in self.windows if w.is_active]

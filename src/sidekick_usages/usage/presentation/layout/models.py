"""Shared immutable inputs for usage-panel layout."""

from dataclasses import dataclass

from rich.text import Text

from sidekick_usages.core.models import UsageReport


@dataclass(frozen=True, slots=True)
class ProviderPanelRow:
    """One provider row projected into presentation-only layout data."""

    marker: Text
    label: str
    plan: str
    report: UsageReport | None

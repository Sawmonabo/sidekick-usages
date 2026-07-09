"""Tests for the shared application branding renderables."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console, Group
from rich.text import Text

from sidekick_usages import branding

_LEFT_EYE = (4, 5)
_RIGHT_EYE = (8, 9)


def _render(renderable: object, *, width: int) -> str:
    """Render a branding value without terminal color escapes."""
    output = io.StringIO()
    console = Console(
        file=output,
        width=width,
        force_terminal=False,
        legacy_windows=False,
    )
    console.print(renderable)
    return output.getvalue()


def test_canonical_robot_rows_are_exact() -> None:
    assert branding.ROBOT_LINES == (
        "      o",
        "     .-.",
        "  .--┴-┴--.",
        "  | O   O |",
        "  | ||||| |",
        "  '--___--'",
    )


def test_full_header_contains_approved_copy_at_minimum_width() -> None:
    assert branding.FULL_HEADER_MIN_WIDTH == 79  # noqa: PLR2004
    out = _render(
        branding.brand_header(branding.FULL_HEADER_MIN_WIDTH),
        width=branding.FULL_HEADER_MIN_WIDTH,
    )
    assert "  .--┴-┴--.    sidekick usages" in out
    assert (
        "  | O   O |   >> A multi-account usage dashboard for "
        "Claude Code and Codex CLI."
    ) in out
    assert (
        "  | ||||| |   >> Limits + resets + account status, one terminal."
        in out
    )
    assert max(len(line) for line in out.splitlines()) == 79  # noqa: PLR2004


@pytest.mark.parametrize(
    ("width", "expected", "excluded"),
    [
        (40, "  .--┴-┴--.  sidekick usages", branding.BRAND_DESCRIPTION),
        (20, "sidekick usages", branding.ROBOT_LINES[2]),
    ],
)
def test_header_degrades_without_wrapping(
    width: int,
    expected: str,
    excluded: str,
) -> None:
    out = _render(branding.brand_header(width), width=width)
    assert expected in out
    assert excluded not in out
    assert max(len(line) for line in out.splitlines()) <= width


def test_header_places_section_below_divider() -> None:
    out = _render(
        branding.brand_header(79, section="doctor · account diagnostics"),
        width=79,
    )
    divider = "─" * 79
    assert out.index(divider) < out.index("doctor · account diagnostics")


def test_brand_line_contains_compact_title_and_matching_divider() -> None:
    line = branding.brand_line("update status")
    rendered = line.plain.splitlines()
    assert rendered[0] == "sidekick usages · update status"
    assert rendered[1] == "─" * len(rendered[0])


def test_robot_eyes_and_speech_arrows_use_provider_styles() -> None:
    header = branding.brand_header(79)
    assert isinstance(header, Group)
    renderables = list(header.renderables)
    eye_row = renderables[3]
    mouth_row = renderables[4]
    assert isinstance(eye_row, Text)
    assert isinstance(mouth_row, Text)
    assert any(
        (span.start, span.end) == _LEFT_EYE and span.style == "magenta"
        for span in eye_row.spans
    )
    assert any(
        (span.start, span.end) == _RIGHT_EYE and span.style == "cyan"
        for span in eye_row.spans
    )
    assert any(span.style == "magenta" for span in mouth_row.spans)
    assert any(span.style == "cyan" for span in mouth_row.spans)


def test_robot_roof_has_one_source_definition() -> None:
    package = Path(__file__).parents[1] / "src" / "sidekick_usages"
    roof = branding.ROBOT_LINES[2]
    matches = [
        path
        for path in package.rglob("*.py")
        if roof in path.read_text(encoding="utf-8")
    ]
    assert matches == [package / "branding.py"]

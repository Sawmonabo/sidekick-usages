"""Tests for the shared application branding renderables."""

import io
from pathlib import Path

import pytest
from rich.console import Console, Group
from rich.text import Text

from sidekick_usages.branding import content, rich
from sidekick_usages.branding.theme import CLAUDE_STYLE, CODEX_STYLE

_LEFT_EYE = (4, 5)
_RIGHT_EYE = (8, 9)
_FULL_HEADER_WIDTH = 79


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
    assert content.ROBOT_LINES == (
        "      o",
        "     .-.",
        "  .--┴-┴--.",
        "  | O   O |",
        "  | ||||| |",
        "  '--___--'",
    )


def test_full_header_contains_approved_copy_at_minimum_width() -> None:
    assert content.FULL_HEADER_MIN_WIDTH == _FULL_HEADER_WIDTH
    out = _render(
        rich.brand_header(_FULL_HEADER_WIDTH),
        width=_FULL_HEADER_WIDTH,
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
    assert max(len(line) for line in out.splitlines()) == _FULL_HEADER_WIDTH


@pytest.mark.parametrize(
    ("width", "expected", "excluded"),
    [
        (40, "  .--┴-┴--.  sidekick usages", content.BRAND_DESCRIPTION),
        (20, "sidekick usages", content.ROBOT_LINES[2]),
    ],
)
def test_header_degrades_without_wrapping(
    width: int,
    expected: str,
    excluded: str,
) -> None:
    out = _render(rich.brand_header(width), width=width)
    assert expected in out
    assert excluded not in out
    assert max(len(line) for line in out.splitlines()) <= width


def test_header_places_section_below_divider() -> None:
    out = _render(
        rich.brand_header(79, section="doctor · account diagnostics"),
        width=79,
    )
    divider = "─" * 79
    assert out.index(divider) < out.index("doctor · account diagnostics")


def test_update_status_line_has_compact_title_and_matching_divider() -> None:
    line = rich.update_status_line()
    rendered = line.plain.splitlines()
    assert rendered[0] == "sidekick usages · update status"
    assert rendered[1] == "─" * len(rendered[0])


def test_robot_eyes_and_speech_arrows_use_provider_styles() -> None:
    header = rich.brand_header(79)
    assert isinstance(header, Group)
    renderables = list(header.renderables)
    eye_row = renderables[3]
    mouth_row = renderables[4]
    assert isinstance(eye_row, Text)
    assert isinstance(mouth_row, Text)
    expected_claude_style = rich.rich_style(CLAUDE_STYLE)
    expected_codex_style = rich.rich_style(CODEX_STYLE)
    assert any(
        (span.start, span.end) == _LEFT_EYE
        and span.style == expected_claude_style
        for span in eye_row.spans
    )
    assert any(
        (span.start, span.end) == _RIGHT_EYE
        and span.style == expected_codex_style
        for span in eye_row.spans
    )
    assert any(span.style == expected_claude_style for span in mouth_row.spans)
    assert any(span.style == expected_codex_style for span in mouth_row.spans)


def test_readme_masthead_matches_canonical_branding() -> None:
    """The README logo stays aligned with the runtime source of truth."""
    readme = (Path(__file__).parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    rows = (
        content.ROBOT_LINES[0],
        content.ROBOT_LINES[1],
        f"{content.ROBOT_LINES[2]}    {content.BRAND_TITLE}",
        f"{content.ROBOT_LINES[3]}   >> {content.BRAND_DESCRIPTION}",
        f"{content.ROBOT_LINES[4]}   >> {content.BRAND_PROMISE}",
        content.ROBOT_LINES[5],
    )
    masthead = "```text\n" + "\n".join(rows) + "\n```"

    assert readme.startswith(f"# sidekick-usages\n\n{masthead}\n")

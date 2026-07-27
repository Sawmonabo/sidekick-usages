"""Shared terminal-render assertions."""


def panel_line_widths(out: str) -> set[int]:
    """Return the widths of rendered panel border and interior lines."""
    # ``line and`` guards the blank separator lines: "" is a substring
    # of every string, so "" in "╭│╰" is True and would count width 0.
    return {
        len(line) for line in out.split("\n") if line and line[:1] in "╭│╰"
    }

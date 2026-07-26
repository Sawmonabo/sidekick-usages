"""Canonical actual-terminal geometry acquisition."""

import os
from typing import TextIO

DEFAULT_TERMINAL_WIDTH = 80


def terminal_width(output: TextIO) -> int:
    """Read columns from the output terminal without environment overrides."""
    try:
        width = os.get_terminal_size(output.fileno()).columns
    except AttributeError, OSError, ValueError:
        return DEFAULT_TERMINAL_WIDTH
    return max(1, width)

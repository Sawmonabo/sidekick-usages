"""Acquire tokens through the CLI-owned terminal-input boundary."""

import re
import sys

from rich.console import Console
from rich.prompt import Prompt


class TokenInput:
    """Read and validate one OAuth token from a pipe or hidden prompt."""

    def __init__(
        self,
        token_pattern: re.Pattern[str],
        console: Console | None = None,
    ) -> None:
        self._token_pattern = token_pattern
        self._console = (
            console if console is not None else Console(stderr=True)
        )

    def read(self, prompt: str = "Paste OAuth token") -> str | None:
        """Return a validated token or ``None`` on cancellation."""
        if not sys.stdin.isatty():
            raw = sys.stdin.read()
        else:
            try:
                raw = Prompt.ask(
                    prompt,
                    password=True,
                    console=self._console,
                )
            except EOFError, KeyboardInterrupt:
                return None
        return self.validate(raw)

    def validate(self, raw: str) -> str | None:
        """Extract one provider token from surrounding input."""
        text = raw.strip()
        if not text:
            return None
        match = self._token_pattern.search(text)
        return match.group(0) if match is not None else None


__all__ = ["TokenInput"]

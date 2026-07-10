"""Acquire tokens via stdin or an interactive prompt.

Providers supply their token-shape regex while this CLI-owned boundary
keeps terminal input and presentation outside provider packages.
"""

import re
import sys

from rich.console import Console
from rich.prompt import Prompt


class TokenInput:
    """Reads an OAuth token from the user when --token is omitted.

    Strategy:

    * If stdin is not a TTY (piped), read the token from stdin.
      Example: ``echo $TOKEN | sidekick-usages add claude``.
    * Otherwise prompt interactively with input hidden via
      :class:`rich.prompt.Prompt` so the token doesn't enter shell
      history or appear on screen.
    * Pasted text is matched against a provider-supplied regex to
      extract the token even when surrounded by banner noise.
    """

    def __init__(
        self,
        token_pattern: re.Pattern[str],
        console: Console | None = None,
    ) -> None:
        """:param token_pattern: Compiled regex that matches a
        valid token shape for the provider.

        :param console: Optional Rich console to write status text
            to. Defaults to a stderr-bound console so status doesn't
            mix with piped stdout.
        """
        self._token_pattern = token_pattern
        self._console = console or Console(stderr=True)

    def read(
        self,
        prompt: str = "Paste OAuth token",
    ) -> str | None:
        """Read a token from stdin or an interactive prompt.

        :param prompt: Prompt shown when reading interactively.
        :return: A validated token string, or ``None`` if the user
            cancelled or input failed validation.
        """
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
        """Strip whitespace and extract the token from messy input.

        :param raw: Raw input string (may contain surrounding text).
        :return: Cleaned token, or ``None`` when no match found.
        """
        text = (raw or "").strip()
        if not text:
            return None
        match = self._token_pattern.search(text)
        return match.group(0) if match else None

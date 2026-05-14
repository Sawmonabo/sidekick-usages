"""Acquire tokens via stdin, interactive prompt, or setup-token.

Pulled out of cc-usage.py and generalized so each provider can plug
in its own token-shape regex and its own ``setup-token`` command.
Output goes through a Rich :class:`Console` so all formatting is
consistent with the rest of the CLI.
"""

import re
import shutil
import subprocess
import sys
from collections.abc import Callable

from rich.console import Console
from rich.prompt import Prompt

from sidekick_usages.errors import UsageError


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
        runner: Callable[..., subprocess.CompletedProcess[str]] = (
            subprocess.run
        ),
    ) -> None:
        """:param token_pattern: Compiled regex that matches a
        valid token shape for the provider.

        :param console: Optional Rich console to write status text
            to. Defaults to a stderr-bound console so status doesn't
            mix with piped stdout.
        :param runner: Injectable subprocess runner for tests.
        """
        self._token_pattern = token_pattern
        self._console = console or Console(stderr=True)
        self._runner = runner

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

    def run_setup_command(
        self,
        cmd: list[str],
        cli_name: str,
        timeout: int = 600,
    ) -> str | None:
        """Run a provider's setup-token command and scrape stdout.

        :param cmd: Argv list, e.g. ``["claude", "setup-token"]``.
        :param cli_name: Human-readable CLI name for error messages.
        :param timeout: Seconds to wait for the user to complete
            OAuth.
        :return: A token string, or ``None`` on failure.
        :raises UsageError: When the CLI is not on PATH.
        """
        # Resolve to absolute path via PATH lookup. Using the
        # resolved path avoids B607 (partial executable path) and
        # gives an actionable error when the CLI isn't installed.
        resolved = shutil.which(cmd[0])
        if resolved is None:
            raise UsageError(
                f"The `{cmd[0]}` CLI is not on PATH. Install "
                f"{cli_name} first, then re-run this command."
            )
        full_cmd = [resolved, *cmd[1:]]
        self._console.print(
            f"[dim]Running `{' '.join(cmd)}` — complete the "
            "browser OAuth flow when it opens...[/dim]"
        )
        try:
            result = self._runner(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._console.print(
                f"[red]`{cmd[0]} setup-token` timed out.[/red]"
            )
            return None
        except FileNotFoundError as e:
            raise UsageError(f"The `{cmd[0]}` CLI is not on PATH.") from e

        combined = (result.stdout or "") + "\n" + (result.stderr or "")
        # Echo non-token lines so the user can see what's happening.
        # Filter token lines out to keep them out of scrollback.
        match = self._token_pattern.search(combined)
        for line in combined.splitlines():
            if match and match.group(0) in line:
                continue
            self._console.print(line, highlight=False)

        if not match:
            if result.returncode != 0:
                self._console.print(
                    f"[red]`{cmd[0]} setup-token` exited with "
                    f"code {result.returncode}.[/red]"
                )
            else:
                self._console.print(
                    f"[red]Could not find a token in the output "
                    f"of `{cmd[0]} setup-token`.[/red]"
                )
            return None
        return match.group(0)

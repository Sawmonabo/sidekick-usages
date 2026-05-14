"""Entrypoint for ``python -m sidekick_usages``."""

import sys

from sidekick_usages.cli import _run_typer

if __name__ == "__main__":
    sys.exit(_run_typer())

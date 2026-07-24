"""Entrypoint for ``python -m sidekick_usages``."""

import sys

from sidekick_usages.cli.app import run

if __name__ == "__main__":
    sys.exit(run())

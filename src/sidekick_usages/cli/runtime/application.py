"""Full Typer application process image."""

import sys
from collections.abc import Sequence

from sidekick_usages.cli.app import run


def main(argv: Sequence[str] | None = None) -> int:
    """Run the complete command application."""
    arguments = None if argv is None else list(argv)
    return run(arguments)


if __name__ == "__main__":
    sys.exit(main())

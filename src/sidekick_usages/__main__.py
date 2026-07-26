"""Entrypoint for ``python -m sidekick_usages``."""

import sys

from sidekick_usages.cli.runtime.bootstrap import main

if __name__ == "__main__":
    sys.exit(main())

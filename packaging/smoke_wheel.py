#!/usr/bin/env python3
"""Build and verify the exact sidekick-usages wheel in isolation."""

import sys
from pathlib import Path

from wheel_verification.cli import main
from wheel_verification.errors import WheelVerificationError

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


if __name__ == "__main__":
    try:
        sys.exit(main(REPOSITORY_ROOT))
    except WheelVerificationError as error:
        sys.stderr.write(f"wheel verification failed: {error}\n")
        sys.exit(1)

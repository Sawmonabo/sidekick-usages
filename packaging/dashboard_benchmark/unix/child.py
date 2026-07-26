"""Fresh-process cached dashboard first-paint trace."""

import os
import sys

from dashboard_benchmark.fixtures import (
    REFERENCE_ACCOUNT_COUNT,
    dashboard_snapshot,
)
from dashboard_benchmark.models import FirstPaintSignal
from dashboard_benchmark.render import render_snapshot

TRACE_MODULE = "dashboard_benchmark.unix.trace"


def main() -> int:
    """Paint cached state, then replace this process with the lookup trace."""
    rendered_bytes = render_snapshot(
        dashboard_snapshot(REFERENCE_ACCOUNT_COUNT)
    )
    sys.stdout.write(
        FirstPaintSignal(
            account_count=REFERENCE_ACCOUNT_COUNT,
            rendered_bytes=rendered_bytes,
        ).encode()
        + "\n"
    )
    sys.stdout.flush()
    os.execv(
        sys.executable,
        (sys.executable, "-m", TRACE_MODULE),
    )


if __name__ == "__main__":
    sys.exit(main())

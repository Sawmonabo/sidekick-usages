"""Unix child-process measurements for the dashboard release trace."""

import os
import resource
import sys

BYTES_PER_KIBIBYTE = 1_024
BYTES_PER_MEBIBYTE = 1_048_576


def peak_reaped_child_rss_bytes() -> int:
    """Return ``ru_maxrss`` for reaped direct children in bytes."""
    peak = int(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    if sys.platform == "darwin":
        return peak
    return peak * BYTES_PER_KIBIBYTE


def all_children_reaped() -> bool:
    """Return whether the process owns no live or waitable children."""
    try:
        os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        return True
    return False

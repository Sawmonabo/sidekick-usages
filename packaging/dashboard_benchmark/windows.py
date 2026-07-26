"""Native-Windows interactive feature-disabled release gate."""

import sys

from dashboard_benchmark.command import execute
from dashboard_benchmark.errors import DashboardBenchmarkError
from sidekick_usages.cli.dashboard.models.setup import (
    ServiceSetupDecision,
    ServiceSetupMessage,
    ServiceSetupOutcome,
)
from sidekick_usages.cli.dashboard.setup import GuidedServiceSetup
from sidekick_usages.daemon.lifecycle.manager import build_daemon_manager
from sidekick_usages.usage.dashboard.models import DashboardService


def main() -> int:
    """Require native Windows to disable interactive account switching."""
    if len(sys.argv) != 1:
        raise DashboardBenchmarkError(
            "Native Windows dashboard gate accepts no arguments."
        )
    intent = "native-windows-platform-gate"
    result = GuidedServiceSetup(build_daemon_manager()).prepare(
        service=DashboardService(
            ready=False,
            compatible=False,
            phase=None,
            observed_at=None,
            failure_code=None,
        ),
        intent=intent,
        interactive=True,
        decision=ServiceSetupDecision.NOT_REQUESTED,
    )
    if (
        result.intent != intent
        or result.outcome is not ServiceSetupOutcome.UNSUPPORTED
        or result.message is not ServiceSetupMessage.UNSUPPORTED
        or result.corrective_action is not None
    ):
        raise DashboardBenchmarkError(
            "Native Windows did not return the feature-disabled dashboard "
            "contract."
        )
    sys.stdout.write(
        "Native Windows dashboard account switching is feature-disabled.\n"
    )
    return 0


if __name__ == "__main__":
    execute(main)

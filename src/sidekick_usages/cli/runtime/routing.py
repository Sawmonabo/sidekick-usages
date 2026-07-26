"""Typed private-dashboard argument routing."""

from collections.abc import Sequence

from sidekick_usages.core.types import ProviderId

ONLY_OPTION = "--only"
ONLY_ARGUMENT_COUNT = 2


def dashboard_candidate(arguments: Sequence[str]) -> bool:
    """Return whether arguments identify one valid dashboard route."""
    try:
        parse_dashboard_arguments(arguments)
    except ValueError:
        return False
    return True


def parse_dashboard_arguments(
    arguments: Sequence[str],
) -> ProviderId | None:
    """Return the provider from one exact private dashboard invocation."""
    if not arguments:
        return None
    if len(arguments) == ONLY_ARGUMENT_COUNT and arguments[0] == ONLY_OPTION:
        return ProviderId(arguments[1])
    if len(arguments) == 1 and arguments[0].startswith(f"{ONLY_OPTION}="):
        return ProviderId(arguments[0].removeprefix(f"{ONLY_OPTION}="))
    raise ValueError("Invalid dashboard invocation.")


def dashboard_arguments(only: ProviderId | None) -> tuple[str, ...]:
    """Return canonical private-dashboard arguments."""
    if only is None:
        return ()
    return (ONLY_OPTION, only.value)

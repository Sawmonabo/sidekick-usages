"""Self-update support for the CLI.

Two surfaces:

* :func:`fetch_latest_release` — GETs the GitHub Releases ``/latest``
  endpoint and returns the version string with the leading ``v``
  stripped. Used by the ``check-update`` subcommand.
* :func:`detect_install_method` + :func:`upgrade_command_for` —
  inspect ``sys.executable`` to pick the upgrade invocation
  (``uv tool upgrade``, ``pipx upgrade``, ``brew upgrade``). Used by
  the ``update`` subcommand.

Version comparison is intentionally lightweight (tuple of ints).
release-please is configured with ``prerelease: false`` so we never
see PEP-440 pre-release or build-metadata segments and don't need
the ``packaging`` dependency.
"""

import enum
import sys
from pathlib import Path

from sidekick_usages.http import HttpClient

#: GitHub Releases API endpoint for the canonical repo. Returns the
#: most recent non-draft, non-prerelease release as JSON.
RELEASES_URL = (
    "https://api.github.com/repos/Sawmonabo/sidekick-usages/releases/latest"
)

#: Distribution name used in upgrade commands. Matches
#: ``[project].name`` in pyproject.toml and the Homebrew formula slug.
PACKAGE_NAME = "sidekick-usages"

#: Accept header recommended by GitHub's REST API docs.
_GITHUB_ACCEPT = "application/vnd.github+json"


class InstallMethod(enum.Enum):
    """Discriminated install-source tag.

    Members map 1:1 to upgrade commands in
    :func:`upgrade_command_for`. ``UNKNOWN`` is the explicit fallback
    — we refuse to guess at an upgrade command and show manual
    instructions instead.
    """

    UV = "uv"
    PIPX = "pipx"
    HOMEBREW = "homebrew"
    UNKNOWN = "unknown"


def fetch_latest_release(http: HttpClient) -> str:
    """Fetch the latest release tag name from GitHub.

    :param http: Configured HTTP client (handles retries + typed
        errors). No auth is required — the endpoint is public.
    :return: Version string with the leading ``v`` removed,
        e.g. ``"0.3.0"`` for tag ``v0.3.0``.
    :raises ForbiddenError: When the GitHub anonymous rate limit
        (60/hr) is exhausted. Caller renders as a hint.
    :raises TransientError: For 404 (no release yet) or other
        non-success responses after retries.
    :raises ValueError: When ``tag_name`` is missing or not a string.
    """
    payload = http.get_json(RELEASES_URL, {"Accept": _GITHUB_ACCEPT})
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise ValueError(f"Unexpected tag_name shape: {tag!r}")
    return tag.removeprefix("v")


def parse_version(s: str) -> tuple[int, ...]:
    """Parse a semver-ish version string into a tuple of ints.

    Tolerant of a leading ``v``. Does NOT handle PEP-440 pre-release
    or build-metadata segments — release-please cannot emit those
    with the current config.

    :param s: Version string (``"0.3.0"`` or ``"v0.3.0"``).
    :return: Tuple suitable for direct comparison
        (e.g. ``(0, 3, 0)``).
    :raises ValueError: When any segment is non-numeric.
    """
    cleaned = s.removeprefix("v").strip()
    return tuple(int(part) for part in cleaned.split("."))


def is_newer(latest: str, current: str) -> bool:
    """Return ``True`` iff ``latest`` is strictly newer than ``current``.

    :param latest: Remote version string.
    :param current: Local version string.
    :return: ``True`` when ``latest`` > ``current``.
    """
    return parse_version(latest) > parse_version(current)


def detect_install_method(
    executable: str | Path | None = None,
) -> InstallMethod:
    """Sniff the install source from the Python executable path.

    Heuristic only — no subprocesses, no filesystem reads. Order
    matters: Homebrew sometimes installs into a venv whose path
    contains other markers, so Homebrew is checked first.

    :param executable: Path to inspect (defaults to
        :data:`sys.executable`).
    :return: The detected :class:`InstallMethod`.
    """
    raw = str(executable if executable is not None else sys.executable)
    parts = Path(raw).parts
    if any(p in {"Cellar", "linuxbrew"} for p in parts):
        return InstallMethod.HOMEBREW
    if "uv" in parts and "tools" in parts:
        return InstallMethod.UV
    if "pipx" in parts:
        return InstallMethod.PIPX
    return InstallMethod.UNKNOWN


def upgrade_command_for(method: InstallMethod) -> tuple[str, ...]:
    """Map an install method to its upgrade argv.

    :param method: Detected install method.
    :return: argv tuple ready to pass to :func:`subprocess.run`.
    :raises ValueError: When ``method`` is ``UNKNOWN`` (caller should
        check first and surface :func:`manual_instructions`).
    """
    if method is InstallMethod.UV:
        return ("uv", "tool", "upgrade", PACKAGE_NAME)
    if method is InstallMethod.PIPX:
        return ("pipx", "upgrade", PACKAGE_NAME)
    if method is InstallMethod.HOMEBREW:
        return ("brew", "upgrade", PACKAGE_NAME)
    raise ValueError(f"No upgrade command for install method {method.value!r}")


def manual_instructions() -> str:
    """User-facing help text for the ``UNKNOWN`` install case.

    :return: Multi-line string listing the supported upgrade paths.
    """
    return (
        "Could not detect how sidekick-usages is installed. "
        "Run one of these manually:\n"
        "  uv tool upgrade sidekick-usages\n"
        "  pipx upgrade sidekick-usages\n"
        "  brew upgrade sidekick-usages"
    )

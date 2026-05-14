#!/usr/bin/env python3
"""Generate the sidekick-usages Homebrew formula.

Reads the project version from ``pyproject.toml`` (or ``--version``),
resolves the runtime dependency closure via ``uv pip compile``,
fetches the matching sdist URL + sha256 from PyPI's JSON API for
each dep, hashes the GitHub-archive source tarball, and emits a
complete Ruby formula.

This is the single source of truth for the formula content. Both
``packaging/homebrew/sidekick-usages.rb`` (in-tree template) and
``Sawmonabo/homebrew-tap:Formula/sidekick-usages.rb`` (what end users
install) should be byte-identical to this generator's output.

The companion workflow ``.github/workflows/bump-homebrew.yml`` runs
this on every ``v*`` tag push and opens PRs against both files.

Local usage::

    uv run packaging/homebrew/generate.py --output /tmp/sidekick-usages.rb
    diff /tmp/sidekick-usages.rb packaging/homebrew/sidekick-usages.rb
"""

import argparse
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tomllib
import urllib.request

#: Runtime deps the wheel pulls in. Listed explicitly (rather than
#: discovered) so the order of ``resource`` blocks in the formula is
#: stable across releases and reviewers can diff by version, not by
#: shuffled order.
RUNTIME_DEPS: tuple[str, ...] = (
    "annotated-doc",
    "click",
    "markdown-it-py",
    "mdurl",
    "pygments",
    "rich",
    "shellingham",
    "typer",
)

PYPI_JSON_URL = "https://pypi.org/pypi/{pkg}/{ver}/json"
GH_ARCHIVE_URL = (
    "https://github.com/Sawmonabo/sidekick-usages"
    "/archive/refs/tags/{tag}.tar.gz"
)
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def project_version() -> str:
    """Read ``project.version`` from ``pyproject.toml``.

    :return: The version string as declared in pyproject.
    :raises KeyError: If the field is missing.
    """
    pyproject = REPO_ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    return str(data["project"]["version"])


def resolved_versions(deps: tuple[str, ...]) -> dict[str, str]:
    """Resolve runtime deps to pinned versions via ``uv pip compile``.

    :param deps: PEP 503-normalized names of the runtime deps to
        resolve. Anything in ``uv pip compile``'s output that is
        outside this set is ignored.
    :return: A mapping of name -> version (e.g. ``{"click": "8.3.3"}``)
        covering every name in ``deps``.
    :raises RuntimeError: If any name in ``deps`` is missing from
        ``uv pip compile``'s output (likely a typo or pyproject drift),
        or if the ``uv`` binary is not on PATH.
    """
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        raise RuntimeError("`uv` is not on PATH; install Astral uv first.")
    proc = subprocess.run(
        [
            uv_bin,
            "pip",
            "compile",
            "--quiet",
            "--no-header",
            str(REPO_ROOT / "pyproject.toml"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    versions: dict[str, str] = {}
    for raw in proc.stdout.splitlines():
        # Strip inline comments (`# via foo`)
        line = raw.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, ver = line.split("==", 1)
        normalized = name.strip().lower().replace("_", "-")
        # Drop environment markers / extras after the version
        ver = ver.split(" ", 1)[0].split(";", 1)[0].strip()
        versions[normalized] = ver

    missing = sorted(set(deps) - set(versions))
    if missing:
        raise RuntimeError(
            f"`uv pip compile` did not resolve {missing}. "
            f"Available: {sorted(versions)}"
        )
    return {pkg: versions[pkg] for pkg in deps}


def pypi_sdist(pkg: str, ver: str) -> tuple[str, str]:
    """Look up the sdist URL + sha256 for ``pkg==ver`` on PyPI.

    Falls back to the first wheel if no sdist is published, with a
    stderr warning. Homebrew's ``virtualenv_install_with_resources``
    prefers sdists because it builds from source.

    :param pkg: Package name (case-insensitive, normalized form).
    :param ver: Exact version string.
    :return: ``(url, sha256)`` of the artifact PyPI ships.
    :raises RuntimeError: If neither sdist nor wheel is published.
    """
    url = PYPI_JSON_URL.format(pkg=pkg, ver=ver)
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.load(resp)
    files = data.get("urls", [])
    sdists = [f for f in files if f["packagetype"] == "sdist"]
    if sdists:
        chosen = sdists[0]
    else:
        wheels = [f for f in files if f["packagetype"] == "bdist_wheel"]
        if not wheels:
            raise RuntimeError(f"No distributions for {pkg}=={ver}")
        sys.stderr.write(f"WARNING: {pkg} {ver} has no sdist; using wheel\n")
        chosen = wheels[0]
    return chosen["url"], chosen["digests"]["sha256"]


def archive_sha256(tag: str) -> str:
    """Stream the GitHub-archive tarball for ``tag`` and hash it.

    :param tag: Git tag (e.g. ``"v0.1.0"``).
    :return: Lowercase hex sha256 of the tarball bytes.
    """
    url = GH_ARCHIVE_URL.format(tag=tag)
    hasher = hashlib.sha256()
    with urllib.request.urlopen(url, timeout=60) as resp:
        for chunk in iter(lambda: resp.read(64 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def format_resource(pkg: str, url: str, sha: str) -> str:
    """Render a single ``resource`` block in canonical Homebrew style."""
    return f'  resource "{pkg}" do\n    url "{url}"\n    sha256 "{sha}"\n  end'


def emit_formula(
    version: str,
    archive_sha: str,
    resources: list[tuple[str, str, str]],
) -> str:
    """Build the full ``.rb`` file as a single string.

    :param version: Project version without the ``v`` prefix.
    :param archive_sha: sha256 of the GitHub-archive tarball.
    :param resources: List of ``(name, url, sha256)`` tuples in the
        order they should appear in the formula.
    :return: The formula's Ruby source, terminated by a newline.
    """
    blocks = "\n\n".join(format_resource(p, u, s) for p, u, s in resources)
    # Note: ``#{{var}}`` -> literal ``#{var}`` in Ruby's interpolation.
    return f"""# typed: false
# frozen_string_literal: true

# Sidekick-Usages - Homebrew formula.
#
# Auto-generated by packaging/homebrew/generate.py - do not edit by hand.
# Regenerate after a release with:
#
#   uv run packaging/homebrew/generate.py --output <path>
#
# End-user install:
#
#   brew tap Sawmonabo/tap
#   brew install sidekick-usages
class SidekickUsages < Formula
  include Language::Python::Virtualenv

  desc "Check Claude Code and Codex CLI usage across multiple accounts"
  homepage "https://github.com/Sawmonabo/sidekick-usages"
  url "https://github.com/Sawmonabo/sidekick-usages/archive/refs/tags/v{version}.tar.gz"
  sha256 "{archive_sha}"
  license "Apache-2.0"
  head "https://github.com/Sawmonabo/sidekick-usages.git", branch: "main"

  depends_on "python@3.14"

  # Runtime deps; versions match `uv pip compile pyproject.toml`.
{blocks}

  def install
    virtualenv_install_with_resources
  end

  test do
    # Verify the binary runs and reports its version.
    assert_match "sidekick-usages #{{version}}", shell_output("#{{bin}}/sidekick-usages --version")

    # `list` with no saved accounts must exit 0 and print the empty-state hint.
    output = shell_output("#{{bin}}/sidekick-usages list 2>&1", 0)
    assert_match(/no accounts saved/i, output)
  end
end
"""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the sidekick-usages Homebrew formula.",
    )
    parser.add_argument(
        "--version",
        help="Version to bump to (default: read from pyproject.toml).",
    )
    parser.add_argument(
        "--output",
        help="Write formula to this path (default: stdout).",
    )
    args = parser.parse_args()

    version = args.version or project_version()
    tag = f"v{version}"

    sys.stderr.write(f"==> Hashing source archive {tag}\n")
    archive = archive_sha256(tag)
    sys.stderr.write(f"    sha256 = {archive}\n")

    sys.stderr.write("==> Resolving runtime deps via `uv pip compile`\n")
    versions = resolved_versions(RUNTIME_DEPS)
    for pkg, ver in versions.items():
        sys.stderr.write(f"    {pkg}=={ver}\n")

    sys.stderr.write("==> Fetching PyPI sdist URLs/SHAs\n")
    resources: list[tuple[str, str, str]] = []
    for pkg in RUNTIME_DEPS:
        ver = versions[pkg]
        url, sha = pypi_sdist(pkg, ver)
        resources.append((pkg, url, sha))

    formula = emit_formula(version, archive, resources)
    if args.output:
        out = pathlib.Path(args.output)
        out.write_text(formula)
        sys.stderr.write(f"\nWrote {out} ({len(formula)} bytes)\n")
    else:
        sys.stdout.write(formula)
    return 0


if __name__ == "__main__":
    sys.exit(main())

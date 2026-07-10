#!/usr/bin/env python3
"""Generate the sidekick-usages Homebrew formula.

Reads the project version from ``pyproject.toml`` (or ``--version``),
constrains the runtime dependency closure to ``uv.lock``,
fetches the matching sdist URL + sha256 from PyPI's JSON API for
each dep, hashes the selected source archive, and emits a complete
Ruby formula.

Source builds with native backends are fail-closed behind reviewed
build-tool metadata. Pydantic-core 2.46.4 is approved with maturin
``>=1.10,<2`` and Rust ``>=1.88``; a different core version requires
a fresh packaging review before formula generation can continue.

This is the source of truth for formula generation. The checked-in formula
and the tap formula remain tied to the latest released tag; the release
workflow regenerates both from that tag while development can move ahead.

The companion workflow ``.github/workflows/bump-homebrew.yml`` runs
this on every ``v*`` tag push and opens PRs against both files.

Release-tag usage::

    uv run packaging/homebrew/generate.py --output /tmp/sidekick-usages.rb
    diff /tmp/sidekick-usages.rb packaging/homebrew/sidekick-usages.rb

Use ``--source-archive`` to generate a formula for unreleased source without
rewriting the formula tied to the latest release.
"""

import argparse
import contextlib
import dataclasses
import hashlib
import http.client
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
import urllib.request
from collections.abc import Iterator
from typing import IO
from urllib.parse import SplitResult, urlsplit

PYPI_JSON_URL = "https://pypi.org/pypi/{pkg}/{ver}/json"
GH_ARCHIVE_URL = (
    "https://github.com/Sawmonabo/sidekick-usages"
    "/archive/refs/tags/{tag}.tar.gz"
)
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_REVIEWED_PYDANTIC_CORE = "2.46.4"
_PYPI_JSON_MAX_BYTES = 1024 * 1024
_PYDANTIC_CORE_BUILD_DEPENDENCIES = (
    ("maturin", ">=1.10,<2"),
    ("rust", ">=1.88"),
)


@dataclasses.dataclass(frozen=True, slots=True)
class _PyPIReleaseFile:
    """Validated PyPI release-file fields used by formula generation."""

    package_type: str
    filename: str
    url: str
    sha256: str


def _require_https(url: str) -> SplitResult:
    """Reject non-HTTPS URLs and URLs containing credentials."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise RuntimeError(
            "Generator network access requires a valid HTTPS URL "
            "without credentials."
        ) from None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or port == 0
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RuntimeError(
            "Generator network access requires a valid HTTPS URL "
            "without credentials."
        )
    return parsed


def _require_safe_resource_url(url: str) -> None:
    """Reject URL content that could alter generated Ruby source."""
    parsed = _require_https(url)
    if (
        parsed.fragment
        or '"' in url
        or "\\" in url
        or any(unicodedata.category(char) == "Cc" for char in url)
    ):
        raise RuntimeError("PyPI package metadata has an unsafe resource URL.")


def _require_sha256(digest: str) -> None:
    """Require a lowercase hexadecimal SHA-256 digest."""
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise RuntimeError(
            "PyPI package metadata has an invalid SHA-256 digest."
        )


class _HttpsOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject a redirect before urllib can follow it outside HTTPS."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Validate a redirect target before constructing its request."""
        _require_https(newurl)
        return super().redirect_request(
            req,
            fp,
            code,
            msg,
            headers,
            newurl,
        )


@contextlib.contextmanager
def _open_https(
    url: str,
    *,
    timeout: float,
    purpose: str,
) -> Iterator[IO[bytes]]:
    """Open one non-retrying HTTPS stream with credential-safe errors."""
    _require_https(url)
    opener = urllib.request.build_opener(_HttpsOnlyRedirectHandler())
    try:
        with opener.open(url, timeout=timeout) as response:
            yield response
    except OSError, ValueError, http.client.HTTPException:
        raise RuntimeError(f"HTTPS fetch failed for {purpose}.") from None
    finally:
        opener.close()


def _read_pypi_release_files(response: IO[bytes]) -> list[_PyPIReleaseFile]:
    """Decode bounded PyPI JSON into the fields used by the generator."""
    payload = response.read(_PYPI_JSON_MAX_BYTES + 1)
    if len(payload) > _PYPI_JSON_MAX_BYTES:
        raise RuntimeError("PyPI package metadata exceeds the size limit.")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError, UnicodeDecodeError:
        raise RuntimeError(
            "PyPI package metadata is not valid JSON."
        ) from None
    if not isinstance(data, dict):
        raise RuntimeError("PyPI package metadata must be a JSON object.")
    raw_files = data.get("urls")
    if not isinstance(raw_files, list):
        raise RuntimeError("PyPI package metadata has no release file list.")

    files: list[_PyPIReleaseFile] = []
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise RuntimeError("PyPI package metadata has an invalid file.")
        package_type = raw_file.get("packagetype")
        filename = raw_file.get("filename")
        url = raw_file.get("url")
        digests = raw_file.get("digests")
        if (
            not isinstance(package_type, str)
            or not isinstance(filename, str)
            or not isinstance(url, str)
            or not isinstance(digests, dict)
            or not isinstance(sha256 := digests.get("sha256"), str)
        ):
            raise RuntimeError("PyPI package metadata has an invalid file.")
        _require_safe_resource_url(url)
        _require_sha256(sha256)
        files.append(
            _PyPIReleaseFile(
                package_type=package_type,
                filename=filename,
                url=url,
                sha256=sha256,
            )
        )
    return files


def project_version() -> str:
    """Read ``project.version`` from ``pyproject.toml``.

    :return: The version string as declared in pyproject.
    :raises KeyError: If the field is missing.
    """
    pyproject = REPO_ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    return str(data["project"]["version"])


def parse_resolved_versions(output: str) -> list[tuple[str, str]]:
    """Parse package pins from ``uv pip compile`` output.

    ``uv`` only emits packages that are part of the current resolved
    dependency graph. Keeping this list dynamic prevents the Homebrew
    generator from drifting when a dependency adds or removes a
    transitive dependency.

    :param output: Raw stdout from ``uv pip compile``.
    :return: Ordered ``(normalized_name, version)`` tuples.
    """
    versions: list[tuple[str, str]] = []
    for raw in output.splitlines():
        # Strip inline comments (`# via foo`)
        line = raw.split("#", 1)[0].strip()
        if not line or "==" not in line:
            continue
        name, ver = line.split("==", 1)
        normalized = name.strip().lower().replace("_", "-")
        # Drop environment markers / extras after the version
        version = ver.split(" ", 1)[0].split(";", 1)[0].strip()
        versions.append((normalized, version))
    return versions


def resolved_versions() -> list[tuple[str, str]]:
    """Resolve platform runtime deps at the versions pinned by ``uv.lock``.

    :return: Ordered ``(name, version)`` tuples for every package in the
        project runtime dependency closure.
    :raises RuntimeError: If ``uv`` is not on PATH or no runtime
        packages are resolved.
    """
    uv_bin = shutil.which("uv")
    if uv_bin is None:
        raise RuntimeError("`uv` is not on PATH; install Astral uv first.")
    with tempfile.TemporaryDirectory(prefix="sidekick-homebrew-lock-") as raw:
        constraints = pathlib.Path(raw) / "runtime-constraints.txt"
        subprocess.run(
            [
                uv_bin,
                "export",
                "--locked",
                "--no-default-groups",
                "--no-emit-project",
                "--no-header",
                "--no-hashes",
                "--no-annotate",
                "--format",
                "requirements-txt",
                "--output-file",
                str(constraints),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        proc = subprocess.run(
            [
                uv_bin,
                "pip",
                "compile",
                "--quiet",
                "--no-header",
                "--constraint",
                str(constraints),
                str(REPO_ROOT / "pyproject.toml"),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )

    versions = parse_resolved_versions(proc.stdout)
    if not versions:
        raise RuntimeError(
            "`uv pip compile` did not resolve runtime packages."
        )
    return versions


def pypi_sdist(pkg: str, ver: str) -> tuple[str, str, bool]:
    """Look up the sdist URL + sha256 for ``pkg==ver`` on PyPI.

    :param pkg: Package name (case-insensitive, normalized form).
    :param ver: Exact version string.
    :return: ``(url, sha256, native_source_build)``. A source build is
        conservatively native when the release has no universal wheel.
    :raises RuntimeError: If no source distribution is published.
    """
    url = PYPI_JSON_URL.format(pkg=pkg, ver=ver)
    with _open_https(
        url,
        timeout=15,
        purpose="PyPI package metadata",
    ) as resp:
        files = _read_pypi_release_files(resp)
    sdists = [f for f in files if f.package_type == "sdist"]
    if not sdists:
        raise RuntimeError(f"No source distribution for {pkg}=={ver}")
    chosen = sdists[0]
    wheels = [f for f in files if f.package_type == "bdist_wheel"]
    universal_suffixes = ("-py3-none-any.whl", "-py2.py3-none-any.whl")
    has_universal_wheel = any(
        wheel.filename.endswith(universal_suffixes) for wheel in wheels
    )
    return (
        chosen.url,
        chosen.sha256,
        not has_universal_wheel,
    )


def reviewed_build_dependencies(
    native_source_builds: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Return reviewed Homebrew source-build tools for the closure.

    :param native_source_builds: Releases without a universal wheel.
    :return: Ordered ``(formula, upstream_requirement)`` tuples.
    :raises RuntimeError: If a native source build lacks reviewed metadata.
    """
    if not native_source_builds:
        return []
    reviewed = [("pydantic-core", _REVIEWED_PYDANTIC_CORE)]
    if native_source_builds != reviewed:
        raise RuntimeError(
            "Unreviewed native source build closure: "
            f"{native_source_builds!r}; expected {reviewed!r}."
        )
    return list(_PYDANTIC_CORE_BUILD_DEPENDENCIES)


def archive_sha256(tag: str) -> str:
    """Stream the GitHub-archive tarball for ``tag`` and hash it.

    :param tag: Git tag (e.g. ``"v0.1.0"``).
    :return: Lowercase hex sha256 of the tarball bytes.
    """
    url = GH_ARCHIVE_URL.format(tag=tag)
    hasher = hashlib.sha256()
    with _open_https(
        url,
        timeout=60,
        purpose="GitHub release archive",
    ) as resp:
        for chunk in iter(lambda: resp.read(64 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def local_archive_source(path: pathlib.Path) -> tuple[str, str]:
    """Return a local source archive's file URI and sha256.

    :param path: Existing source archive produced from the current checkout.
    :return: ``(file_uri, sha256)``.
    :raises RuntimeError: If ``path`` is not a regular file.
    """
    archive = path.expanduser().resolve()
    if not archive.is_file():
        raise RuntimeError(f"Source archive is not a file: {archive}")
    hasher = hashlib.sha256()
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            hasher.update(chunk)
    return archive.as_uri(), hasher.hexdigest()


def format_resource(pkg: str, url: str, sha: str) -> str:
    """Render a single ``resource`` block in canonical Homebrew style."""
    _require_safe_resource_url(url)
    _require_sha256(sha)
    return f'  resource "{pkg}" do\n    url "{url}"\n    sha256 "{sha}"\n  end'


def emit_formula(
    version: str,
    source_url: str,
    archive_sha: str,
    resources: list[tuple[str, str, str]],
    build_dependencies: list[tuple[str, str]],
) -> str:
    """Build the full ``.rb`` file as a single string.

    :param version: Project version without the ``v`` prefix.
    :param source_url: Release URL or local file URI for the source archive.
    :param archive_sha: sha256 of the selected source archive.
    :param resources: List of ``(name, url, sha256)`` tuples in the
        order they should appear in the formula.
    :param build_dependencies: Reviewed Homebrew source-build tools with
        their upstream version requirements.
    :return: The formula's Ruby source, terminated by a newline.
    """
    blocks = "\n\n".join(format_resource(p, u, s) for p, u, s in resources)
    build_block = ""
    if build_dependencies:
        requirements = "; ".join(
            f"{name} {requirement}" for name, requirement in build_dependencies
        )
        declarations = "\n".join(
            f'  depends_on "{name}" => :build'
            for name, _ in build_dependencies
        )
        build_block = (
            "\n  # Reviewed pydantic-core 2.46.4 source-build tools: "
            f"{requirements}.\n{declarations}"
        )
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
  url "{source_url}"
  version "{version}"
  sha256 "{archive_sha}"
  license "Apache-2.0"
  head "https://github.com/Sawmonabo/sidekick-usages.git", branch: "main"

  depends_on "python@3.14"
{build_block}

  # Runtime deps; versions are constrained to `uv.lock`.
{blocks}

  def install
    virtualenv_install_with_resources
  end

  test do
    # Verify the binary runs and reports its version.
    assert_match(
      "sidekick-usages #{{version}}",
      shell_output("#{{bin}}/sidekick-usages --version"),
    )

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
    parser.add_argument(
        "--source-archive",
        type=pathlib.Path,
        help=(
            "Use this current-checkout archive for a local Homebrew "
            "source-build verification."
        ),
    )
    args = parser.parse_args()

    version = args.version or project_version()
    tag = f"v{version}"

    if args.source_archive is None:
        source_url = GH_ARCHIVE_URL.format(tag=tag)
        sys.stderr.write(f"==> Hashing source archive {tag}\n")
        archive = archive_sha256(tag)
    else:
        sys.stderr.write(
            f"==> Hashing local source archive {args.source_archive}\n"
        )
        source_url, archive = local_archive_source(args.source_archive)
    sys.stderr.write(f"    sha256 = {archive}\n")

    sys.stderr.write("==> Resolving locked runtime dependency closure\n")
    versions = resolved_versions()
    for pkg, ver in versions:
        sys.stderr.write(f"    {pkg}=={ver}\n")

    sys.stderr.write("==> Fetching PyPI sdist URLs/SHAs\n")
    resources: list[tuple[str, str, str]] = []
    native_source_builds: list[tuple[str, str]] = []
    for pkg, ver in versions:
        url, sha, is_native = pypi_sdist(pkg, ver)
        resources.append((pkg, url, sha))
        if is_native:
            native_source_builds.append((pkg, ver))

    build_dependencies = reviewed_build_dependencies(native_source_builds)
    formula = emit_formula(
        version,
        source_url,
        archive,
        resources,
        build_dependencies,
    )
    if args.output:
        out = pathlib.Path(args.output)
        out.write_text(formula)
        sys.stderr.write(f"\nWrote {out} ({len(formula)} bytes)\n")
    else:
        sys.stdout.write(formula)
    return 0


if __name__ == "__main__":
    sys.exit(main())

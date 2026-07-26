import hashlib
import http.client
import io
import json
import pathlib
import re
import sys
import tomllib
import urllib.request

import pytest

from homebrew import generate

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_parse_resolved_versions_keeps_pins_and_normalizes_names() -> None:
    output = """\
pydantic==2.13.4
    # via sidekick-usages
pydantic_core==2.46.4
    # via pydantic
urllib3==2.7.0 ; python_version >= "3.14"
    # via sidekick-usages
"""

    assert generate.parse_resolved_versions(output) == [
        ("pydantic", "2.13.4"),
        ("pydantic-core", "2.46.4"),
        ("urllib3", "2.7.0"),
    ]


def test_locked_runtime_closure_contains_every_host_runtime_dependency() -> (
    None
):
    """Formula resolution cannot omit a direct host dependency."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    locked_packages = tomllib.loads((REPO_ROOT / "uv.lock").read_text())[
        "package"
    ]
    locked = {
        package["name"]: package["version"]
        for package in locked_packages
        if "version" in package
    }
    host_dependencies = {
        re.split(r"[\s;<>=!~\[]", requirement, maxsplit=1)[0]
        for requirement in pyproject["project"]["dependencies"]
        if sys.platform == "win32"
        or "sys_platform == 'win32'" not in requirement
    }
    resolved = dict(generate.resolved_versions())

    assert host_dependencies <= resolved.keys()
    assert resolved == {name: locked[name] for name in resolved}
    assert {"prompt-toolkit", "wcwidth", "websockets"} <= resolved.keys()
    if sys.platform == "win32":
        assert resolved["pywin32"] == "312"
    else:
        assert "pywin32" not in resolved


def test_pydantic_core_source_build_policy_is_exact_and_fail_closed() -> None:
    native_source_builds = [("pydantic-core", "2.46.4")]

    assert generate.reviewed_build_dependencies(native_source_builds) == [
        ("maturin", ">=1.10,<2"),
        ("rust", ">=1.88"),
    ]
    with pytest.raises(RuntimeError, match="Unreviewed native"):
        generate.reviewed_build_dependencies([("unexpected-native", "1.0")])


def test_generator_network_boundary_rejects_unsafe_inputs() -> None:
    request = urllib.request.Request("https://example.com/release")
    handler = generate._HttpsOnlyRedirectHandler()
    response = io.BytesIO()
    headers = http.client.HTTPMessage()

    rejected_urls = (
        "http://proxy-user:proxy-secret@example.com/archive",
        "https://example.com:not-a-port/archive",
        "https://example.com:70000/archive",
    )
    initial_errors: list[str] = []
    for url in rejected_urls:
        with (
            pytest.raises(RuntimeError) as initial_error,
            generate._open_https(
                url,
                timeout=1,
                purpose="test archive",
            ),
        ):
            pytest.fail("An unsafe URL reached the transport.")
        initial_errors.append(str(initial_error.value))
    with pytest.raises(RuntimeError) as redirect_error:
        handler.redirect_request(
            request,
            response,
            302,
            "Found",
            headers,
            "http://proxy-user:proxy-secret@example.com/archive",
        )
    invalid_metadata = (
        (b"x" * (generate._PYPI_JSON_MAX_BYTES + 1), "size limit"),
        (b"[]", "JSON object"),
    )
    for payload, message in invalid_metadata:
        with pytest.raises(RuntimeError, match=message):
            generate._read_pypi_release_files(io.BytesIO(payload))
    valid_file = {
        "packagetype": "sdist",
        "filename": "package-1.0.tar.gz",
        "url": "https://files.pythonhosted.org/package-1.0.tar.gz",
        "digests": {"sha256": "a" * 64},
    }
    unsafe_fields = (
        ({"digests": {"sha256": "a" * 63}}, "SHA-256"),
        ({"digests": {"sha256": "A" * 64}}, "SHA-256"),
        ({"digests": {"sha256": "g" * 64}}, "SHA-256"),
        ({"url": 'https://example.com/package".tar.gz'}, "resource URL"),
        ({"url": "https://example.com/package\\.tar.gz"}, "resource URL"),
        ({"url": "https://example.com/package\n.tar.gz"}, "resource URL"),
        ({"url": "https://example.com/package.tar.gz#part"}, "resource URL"),
    )
    for overrides, message in unsafe_fields:
        payload = json.dumps({"urls": [valid_file | overrides]}).encode()
        with pytest.raises(RuntimeError, match=message):
            generate._read_pypi_release_files(io.BytesIO(payload))

    assert all("proxy-user" not in error for error in initial_errors)
    assert all("proxy-secret" not in error for error in initial_errors)
    assert "proxy-user" not in str(redirect_error.value)
    assert "proxy-secret" not in str(redirect_error.value)


def test_local_source_formula_renders_versioned_reviewed_build(
    tmp_path: pathlib.Path,
) -> None:
    archive = tmp_path / "sidekick-source.tar.gz"
    archive.write_bytes(b"current checkout archive")
    source_url, archive_sha = generate.local_archive_source(archive)
    resource_names = ["pydantic-core", "urllib3"]
    resource_data = [
        (
            name,
            f"https://files.pythonhosted.org/{name}.tar.gz",
            "0" * 64,
        )
        for name in resource_names
    ]
    formula = generate.emit_formula(
        "0.7.0",
        source_url,
        archive_sha,
        resource_data,
        generate.reviewed_build_dependencies([("pydantic-core", "2.46.4")]),
    )
    rendered_resource_names = set(
        re.findall(r'^  resource "([^"]+)" do$', formula, re.M)
    )

    assert rendered_resource_names == set(resource_names)
    assert f'url "{archive.resolve().as_uri()}"' in formula
    assert 'version "0.7.0"' in formula
    assert archive_sha == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert f'sha256 "{archive_sha}"' in formula
    assert 'depends_on "maturin" => :build' in formula
    assert 'depends_on "rust" => :build' in formula
    assert "maturin >=1.10,<2; rust >=1.88" in formula
    resource_urls = re.findall(r'^    url "([^"]+)"$', formula, re.M)
    assert all(url.endswith(".tar.gz") for url in resource_urls)

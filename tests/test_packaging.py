import tomllib
from pathlib import Path


def test_runtime_dependencies_include_direct_cli_imports() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    dependencies = {
        dependency.split(">", 1)[0].split("=", 1)[0].split("<", 1)[0].strip()
        for dependency in pyproject["project"]["dependencies"]
    }

    assert "click" in dependencies

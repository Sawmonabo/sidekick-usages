import importlib.util
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
GENERATOR_PATH = REPO_ROOT / "packaging" / "homebrew" / "generate.py"


spec = importlib.util.spec_from_file_location(
    "homebrew_generator",
    GENERATOR_PATH,
)
assert spec is not None
homebrew_generator = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(homebrew_generator)


def test_parse_resolved_versions_uses_compile_output_without_click() -> None:
    output = """\
annotated-doc==0.0.4
    # via typer
markdown-it-py==4.2.0
    # via rich
mdurl==0.1.2
    # via markdown-it-py
pygments==2.20.0
    # via rich
rich==15.0.0
    # via
    #   sidekick-usages (pyproject.toml)
    #   typer
shellingham==1.5.4
    # via typer
typer==0.26.7
    # via sidekick-usages (pyproject.toml)
"""

    assert homebrew_generator.parse_resolved_versions(output) == [
        ("annotated-doc", "0.0.4"),
        ("markdown-it-py", "4.2.0"),
        ("mdurl", "0.1.2"),
        ("pygments", "2.20.0"),
        ("rich", "15.0.0"),
        ("shellingham", "1.5.4"),
        ("typer", "0.26.7"),
    ]


def test_parse_resolved_versions_normalizes_names_and_markers() -> None:
    output = """\
markdown_it_py==4.2.0 ; python_version >= "3.14"  # via rich
"""

    assert homebrew_generator.parse_resolved_versions(output) == [
        ("markdown-it-py", "4.2.0"),
    ]

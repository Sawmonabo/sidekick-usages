"""Load-bearing operator-documentation contracts."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OPERATOR_GUIDES = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "claude" / "README.md",
    REPO_ROOT / "docs" / "claude" / "debugging.md",
    REPO_ROOT / "docs" / "claude" / "schema.md",
    REPO_ROOT / "docs" / "token-maintenance.md",
    REPO_ROOT / "docs" / "persistence-and-recovery.md",
    REPO_ROOT / "docs" / "heartbeat.md",
    REPO_ROOT / "docs" / "networking.md",
)
STALE_CURRENT_COMMANDS = (
    "restore-setup-token",
    "migrate accounts",
    "migrate locations",
    "prepare-rollback",
)


def _read_text(path: Path) -> str:
    """Read one tracked UTF-8 document on every supported platform."""
    return path.read_text(encoding="utf-8")


def _heading_anchors(text: str) -> set[str]:
    """Return GitHub-style anchors for the headings in one guide."""
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"#{1,6}\s+(.+?)\s*#*", line)
        if match is None:
            continue
        heading = re.sub(r"[`*_]", "", match.group(1)).lower()
        heading = re.sub(r"[^\w\- ]", "", heading)
        base = re.sub(r"\s+", "-", heading.strip())
        duplicate = counts.get(base, 0)
        counts[base] = duplicate + 1
        anchors.add(base if duplicate == 0 else f"{base}-{duplicate}")
    return anchors


def _prose_without_fenced_code(text: str) -> str:
    """Return Markdown prose without fenced code examples."""
    in_fence = False
    prose: list[str] = []
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            prose.append(line)
    assert not in_fence
    return re.sub(r"`[^`\n]*`", "", "\n".join(prose))


def test_heartbeat_guide_owns_commands_models_and_quota() -> None:
    """The heartbeat guide remains the detailed product contract."""
    maintenance = _read_text(REPO_ROOT / "docs" / "token-maintenance.md")
    heartbeat = _read_text(REPO_ROOT / "docs" / "heartbeat.md")
    normalized = " ".join(heartbeat.split())

    for contract in (
        "sidekick-usages heartbeat enable",
        "sidekick-usages heartbeat --all --quiet",
        "sidekick-usages maintain --quiet",
        "claude-haiku-4-5-20251001",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
        "--target spark",
        "real model request",
        "consumes",
    ):
        assert contract in normalized
    assert "./heartbeat.md" in maintenance
    assert "gpt-5.4-mini" not in maintenance
    assert "gpt-5.3-codex-spark" not in maintenance


def test_operator_guides_describe_only_current_secret_safe_flows() -> None:
    """Current guides expose one storage generation and no sensitive values."""
    combined = "\n".join(_read_text(path) for path in OPERATOR_GUIDES)
    persistence = _read_text(
        REPO_ROOT / "docs" / "persistence-and-recovery.md"
    )
    normalized_persistence = " ".join(persistence.split())
    claude = _read_text(REPO_ROOT / "docs" / "claude" / "README.md")

    for contract in (
        "one current per-user data layout",
        "schema-version-three",
        "protected credential",
        "There is no automatic or hidden conversion",
    ):
        assert contract in normalized_persistence
    for contract in (
        "setup-token credential",
        "subscription-login credential",
        "trusted capture evidence",
    ):
        assert contract in claude
    for stale in STALE_CURRENT_COMMANDS:
        assert stale not in combined

    without_test_email = re.sub(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.test\b",
        "<synthetic-email>",
        combined,
    )
    assert "sk-ant-" not in combined
    assert "eyJ" not in combined
    assert ".agents/" not in combined
    assert (
        re.search(
            r"(?<![A-Za-z0-9])/(?:home|Users)/[^/<>{}\s`]+/",
            without_test_email,
        )
        is None
    )
    assert (
        re.search(
            r"(?i)\b[A-Z]:\\Users\\[^\\<>{}\s`]+\\",
            without_test_email,
        )
        is None
    )
    assert (
        re.search(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
            without_test_email,
        )
        is None
    )


def test_operator_guide_links_resolve_and_schema_reuses_verified_binary() -> (
    None
):
    """Local links resolve and Claude revalidation cannot select a new shim."""
    link = re.compile(r"\[[^]]+\]\(([^)]+)\)")
    for document in OPERATOR_GUIDES:
        text = _read_text(document)
        for target in link.findall(_prose_without_fenced_code(text)):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path_text, _, fragment = target.partition("#")
            resolved = (
                document
                if not path_text
                else (document.parent / path_text).resolve()
            )
            assert resolved.exists(), f"{document}: broken link {target}"
            if fragment and resolved.suffix == ".md":
                anchors = _heading_anchors(_read_text(resolved))
                assert fragment in anchors, (
                    f"{document}: broken anchor {target}"
                )

    schema = _read_text(REPO_ROOT / "docs" / "claude" / "schema.md")
    revalidation = schema.split(
        "### Revalidate the credential field set",
        maxsplit=1,
    )[1].split("## Sidekick Production Boundary", maxsplit=1)[0]
    assert 'strings "$binary"' in revalidation
    assert "command -v claude" not in revalidation
    assert "doctor/manifest-qualified" in revalidation

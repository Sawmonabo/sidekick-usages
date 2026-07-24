"""Documentation coverage checks for user-facing command changes."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_GUIDES = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "claude" / "README.md",
    REPO_ROOT / "docs" / "claude" / "debugging.md",
    REPO_ROOT / "docs" / "claude" / "schema.md",
    REPO_ROOT / "docs" / "token-maintenance.md",
    REPO_ROOT / "docs" / "persistence-and-recovery.md",
    REPO_ROOT / "docs" / "heartbeat.md",
)
ARCHITECTURE_SPEC = (
    REPO_ROOT
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-07-09-maintainable-application-architecture-design.md"
)
IMPLEMENTATION_PLAN = (
    REPO_ROOT
    / "docs"
    / "superpowers"
    / "plans"
    / "2026-07-12-claude-credential-modes-and-refresh-safety.md"
)
CHANGED_DOCUMENTS = (*CLAUDE_GUIDES, ARCHITECTURE_SPEC, IMPLEMENTATION_PLAN)
TASK_SEVEN_COMPLETED_STEP_COUNT = 6
TASK_SEVEN_PENDING_STEP_COUNT = 0


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


def _mermaid_blocks_are_closed(text: str) -> bool:
    """Return whether every Mermaid fence has one closing fence."""
    in_mermaid = False
    for line in text.splitlines():
        if line == "```mermaid":
            if in_mermaid:
                return False
            in_mermaid = True
        elif line == "```" and in_mermaid:
            in_mermaid = False
    return not in_mermaid


def _prose_without_fenced_code(text: str) -> str:
    """Return Markdown prose without fenced code examples."""
    in_fence = False
    prose: list[str] = []
    for line in text.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            prose.append(line)
    return re.sub(r"`[^`\n]*`", "", "\n".join(prose))


def test_heartbeat_guide_owns_commands_models_and_quota() -> None:
    """The heartbeat guide must remain the detailed product contract."""
    maintenance = _read_text(Path("docs/token-maintenance.md"))
    heartbeat = _read_text(Path("docs/heartbeat.md"))
    normalized = " ".join(heartbeat.split())

    required_contracts = (
        "sidekick-usages heartbeat enable",
        "sidekick-usages heartbeat --all --quiet",
        "sidekick-usages maintain --quiet",
        "claude-haiku-4-5-20251001",
        "gpt-5.4-mini",
        "gpt-5.3-codex-spark",
        "--target spark",
        "real model request",
        "consumes",
    )
    for contract in required_contracts:
        assert contract in normalized
    assert "./heartbeat.md" in maintenance
    assert "gpt-5.4-mini" not in maintenance
    assert "gpt-5.3-codex-spark" not in maintenance


def test_claude_guides_cover_final_credential_and_recovery_contracts() -> None:
    """Tracked operator docs describe every final Claude boundary."""
    claude = _read_text(REPO_ROOT / "docs" / "claude" / "README.md")
    debugging = _read_text(REPO_ROOT / "docs" / "claude" / "debugging.md")
    schema = _read_text(REPO_ROOT / "docs" / "claude" / "schema.md")
    maintenance = _read_text(REPO_ROOT / "docs" / "token-maintenance.md")
    persistence = _read_text(
        REPO_ROOT / "docs" / "persistence-and-recovery.md"
    )

    for contract in (
        "setup-token credential",
        "subscription-login credential",
        "restore-setup-token",
        "issue date cannot be recovered",
    ):
        assert contract in claude
    for contract in (
        "access-token expiry",
        "login expiry",
        "five days",
        "one cause",
        "one recovery action",
    ):
        assert contract in debugging
    for contract in (
        "Claude Code 2.1.207",
        "accessToken",
        "refreshToken",
        "expiresAt",
        "refreshTokenExpiresAt",
        "scopes",
        "subscriptionType",
        "rateLimitTier",
        "organizationUuid",
        "revalidate",
        "Do not copy extracted provider source",
    ):
        assert contract in schema
    for contract in (
        "subscription-login labels only",
        "provider refresh credential",
        "credential-derived operation identity",
        "private staging",
        "five-day login-renewal warning",
    ):
        assert contract in maintenance
    for contract in (
        "refresh recovery",
        "sidekick-usages reset",
        "provider/local atomicity",
        "provably older credential",
    ):
        assert contract in persistence


def test_claude_guides_exclude_secret_shapes_and_stale_field_language() -> (
    None
):
    """Changed guides contain no credential-like data or stale mode advice."""
    combined = "\n".join(_read_text(path) for path in CHANGED_DOCUMENTS)
    without_test_email = re.sub(
        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.test\b",
        "<synthetic-email>",
        combined,
    )

    assert "sk-ant-" not in combined
    assert "eyJ" not in combined
    assert ".agents/" not in combined
    assert ".superpowers/" not in combined
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
    assert (
        re.search(
            r"\b(?:acct|org)_(?!test(?:_|-))[A-Za-z0-9_-]{8,}\b",
            without_test_email,
        )
        is None
    )
    assert (
        re.search(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            without_test_email,
            re.IGNORECASE,
        )
        is None
    )
    assert "optional refresh_token" not in combined.lower()
    assert "optional scopes" not in combined.lower()
    assert "scope-based credential" not in combined.lower()
    assert "all Claude credentials can be refreshed" not in combined


def test_changed_guides_have_resolvable_local_links_and_balanced_mermaid() -> (
    None
):
    """Local guide links resolve and every Mermaid fence is complete."""
    link = re.compile(r"\[[^]]+\]\(([^)]+)\)")
    for document in CHANGED_DOCUMENTS:
        text = _read_text(document)
        assert _mermaid_blocks_are_closed(text)
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


def test_architecture_spec_names_final_credential_and_refresh_owners() -> None:
    """The architecture guide names the concrete enforced module family."""
    architecture = _read_text(ARCHITECTURE_SPEC)

    for module in (
        "providers/claude/credential_schemas.py",
        "credentials/claude_lifetime.py",
        "credentials/claude_restore.py",
        "credentials/claude_transitions.py",
        "credentials/refresh.py",
        "persistence/credential_refresh.py",
        "persistence/credential_refresh_artifacts.py",
        "persistence/credential_refresh_merge.py",
        "persistence/credential_refresh_private_stage.py",
        "persistence/schema/refresh.py",
        "persistence/credential_refresh_stage.py",
    ):
        assert module in architecture
    assert "exact equality between the source package" in architecture


def test_schema_revalidation_reuses_the_verified_executable() -> None:
    """Credential corroboration cannot fall back to an unqualified shim."""
    schema = _read_text(REPO_ROOT / "docs" / "claude" / "schema.md")
    revalidation = schema.split(
        "### Revalidate the credential field set",
        maxsplit=1,
    )[1].split("## Sidekick Production Boundary", maxsplit=1)[0]

    assert 'strings "$binary"' in revalidation
    assert "command -v claude" not in revalidation
    assert "doctor/manifest-qualified" in revalidation


def test_plan_status_and_spec_recovery_match_current_version_two() -> None:
    """Implemented task metadata and current recovery prose cannot drift."""
    plan = _read_text(IMPLEMENTATION_PLAN)
    architecture = _read_text(ARCHITECTURE_SPEC)
    normalized_plan = " ".join(plan.split())
    normalized_architecture = " ".join(architecture.split())

    assert ".superpowers/" not in plan
    assert "tests/test_credential_identity.py" in plan
    for task in range(1, 7):
        body = plan.split(f"### Task {task}:", maxsplit=1)[1].split(
            f"### Task {task + 1}:",
            maxsplit=1,
        )[0]
        assert "- [ ]" not in body
        assert "- [x]" in body
    task_seven = plan.split("### Task 7:", maxsplit=1)[1].split(
        "## 12.",
        maxsplit=1,
    )[0]
    assert task_seven.count("- [x]") == TASK_SEVEN_COMPLETED_STEP_COUNT
    assert task_seven.count("- [ ]") == TASK_SEVEN_PENDING_STEP_COUNT
    assert "- [x] Execute sections 14.1 through 14.7" in task_seven
    assert "- [x] Execute section 14.8" in task_seven
    assert "- [x] Stop and present the exact section 14.9" in task_seven
    assert "Tasks 1-7 implemented and verified" in normalized_plan
    assert "Publication was explicitly authorized" in normalized_plan
    assert "the primary implementation commit is `cffb1a3`" in plan
    assert (
        "corrected recovery run remains pending renewed approval" not in plan
    )

    for contract in (
        "<REPOSITORY_ROOT>",
        "Version-two rollback snapshot",
        '"target_schema_version": 2',
        "first authorized persist creates version two",
        "Current version two",
        "validate latest version two",
        "content-addressed v2 snapshot",
        "v0/v1/v2 account backup or snapshot",
        "Implemented and verified; retained as design authority",
        "completed architecture implementation plan",
        "The architecture migration is complete.",
        "historical execution and verification evidence",
    ):
        assert contract in normalized_architecture
    for stale in (
        "/home/",
        "first authorized persist creates version one",
        "Current version one | Construct",
        "validate latest version one",
        "content-addressed v1 snapshot",
        "valid version-one authority without",
        "current version-one authority",
        "Next, write the matching implementation plan",
        "later persistence and migration gates remain",
        "Next step:",
        "Execute the existing matching implementation plan",
    ):
        assert stale not in normalized_architecture

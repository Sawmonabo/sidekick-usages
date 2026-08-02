# Task 2 Completion Report

Status: complete on 2026-08-02.

## Scope and safety

- Worktree:
  `/home/sabossedgh/dev/.worktrees/sidekick-usages-hardened-global-selection`
- Branch: `feat/hardened-global-account-selection`
- Task 2 base: `1ecb80617d9dabf1b0ff67f198f4266a31bd5da3`
- Task 1's saved-only `DashboardSnapshot` and `DashboardCursor` contracts remain
  the consumed contracts. No external pseudo-row compatibility was restored.
- No live Sidekick or provider state was accessed. PTY coverage used isolated
  temporary `HOME` and XDG roots with synthetic accounts.
- The installed reporter was neither invoked nor changed.
- CodeRabbit and Atlassian, Jira, and GitLab tooling were not used.

## Outcome

The interactive dashboard now has one terminal owner and a height-aware
semantic layout:

- `bootstrap.main()` immediately replaces the process image for interactive
  dashboard routes; it no longer paints a cached dashboard before
  prompt-toolkit starts.
- The obsolete cached-frame and cursor-repositioning owner
  `cli/dashboard/launch.py` is deleted.
- Prompt-toolkit owns one `HSplit` containing fixed masthead, status, and key
  regions around a filling, scrollable account body.
- The body exposes the focused saved-account line as a hidden semantic cursor,
  so prompt-toolkit keeps the same selected account visible through terminal
  resizes.
- Each render reads rows and columns from prompt-toolkit's current output size,
  takes one atomic session view, and produces all layout fragments from that
  same view.
- Terminals below 24 rows use the compact canonical masthead and typed
  `TERMINAL_TOO_SHORT` status while preserving the key footer and access to
  saved accounts by scrolling.
- `render_dashboard()` remains a finite, escape-free, one-shot renderer by
  joining the same semantic fragments used by the interactive application.

## Cohesion changes

`DashboardLookupCoordinator` now owns lookup worker execution, recoverable
retry, diagnostics, snapshot retry/read handling, and immutable outcome
overlays. `InteractiveDashboardSession` retains navigation, actions, startup
reconciliation, footer state, and result presentation.

Final relevant module sizes:

- `cli/dashboard/application.py`: 149 lines
- `cli/dashboard/lookup.py`: 336 lines
- `cli/dashboard/session.py`: 733 lines
- `render/frame.py`: 242 lines
- `render/models.py`: 62 lines

All Task 2 modules remain below the repository's approximately 800-line
cohesion-review threshold.

## TDD evidence

The required public-route PTY tests were written before the height-aware
implementation. The corrected synthetic WSL fixture produced this intended
RED result:

```text
$ uv run pytest tests/dashboard/test_pty.py -q
collected 7 items
tests/dashboard/test_pty.py FFFFFF. [100%]
6 failed, 1 passed in 10.38s
```

All five required terminal sizes, `(52, 24)`, `(79, 40)`, `(80, 48)`,
`(100, 49)`, and `(120, 60)`, observed three `sidekick usages` mastheads
instead of one. The resize journey also lost the selected saved Codex account
when moving from `(100, 49)` to `(52, 24)`. This proved both duplicate
bootstrap/prompt-toolkit ownership and full-frame clipping at short height.

The finalized logical PTY capture preserves output before prompt-toolkit's
first redraw, then combines it with the last completed visible redraw. It
therefore still detects a duplicate bootstrap painter while correctly treating
prompt-toolkit invalidations as replacement paints rather than scrollback.

## GREEN verification

Focused behavioral verification after the final edits:

```text
$ uv run pytest tests/dashboard/test_pty.py \
    tests/usage/test_dashboard_render.py \
    tests/dashboard/test_state.py \
    tests/dashboard/test_routing.py -q
collected 14 items
tests/dashboard/test_pty.py .......                                      [ 50%]
tests/usage/test_dashboard_render.py ...                                 [ 71%]
tests/dashboard/test_state.py ...                                        [ 92%]
tests/dashboard/test_routing.py .                                        [100%]
14 passed in 12.12s
```

The PTY matrix proves exactly one masthead in logical output, one visible key
footer, a zero exit status, and bounded output at all five required sizes. The
interactive journey proves saved Codex focus remains selected and visible
after the short-height resize. Existing normal-exit and interrupt terminal
restoration proofs remain green. Routing additionally proves six synthetic
failed Unix replacements emit the exact plain launch error with no terminal
escape sequences.

Exact Task 2 lint scope:

```text
$ uv run ruff check src/sidekick_usages/cli/runtime/bootstrap.py \
    src/sidekick_usages/cli/dashboard \
    src/sidekick_usages/usage/presentation/dashboard \
    src/sidekick_usages/branding tests/dashboard tests/usage
All checks passed!
```

Full static type scope:

```text
$ uv run ty check src/ tests/ packaging/
All checks passed!
```

Architecture and hygiene:

```text
$ uv run python packaging/check_architecture.py
Architecture check passed with 3 cohesion warning(s).
warning: src/sidekick_usages/providers/codex/broker/responder.py:1: SIZE002
  module has 826 lines; review cohesion
warning: tests/daemon/test_lifecycle.py:1: SIZE002 module has 990 lines;
  review cohesion
warning: tests/dashboard/test_state.py:1: SIZE002 module has 842 lines;
  review cohesion

$ git diff --check
# exited 0 with no output
```

The three architecture warnings pre-existed Task 2. No Task 2 module is named
by them.

## Benchmark compatibility

Deleting `cli/dashboard/launch.py` and retaining Task 1's saved-only cursor
contract required the synthetic packaging benchmark to stop importing the
deleted cached-frame painter and stop passing the removed `external` field. It
now measures the finite canonical `render_dashboard()` output directly. This
is a compatibility repair to the existing benchmark, not a new benchmark or
runtime behavior.

The top-level installed-console benchmark was intentionally not run through an
installed reporter. Its guard reported:

```text
dashboard benchmark failed: Unix dashboard benchmark requires one installed
console script.
```

The renderer portion was instead exercised directly with the repository code
and a synthetic snapshot:

```text
rendered_bytes=3521
cursor_p95_ns=1681910
```

## Changed ownership surfaces

- Bootstrap routing and deletion of cached terminal painting.
- Prompt-toolkit application layout and terminal-dimension normalization.
- Semantic render models, compact branding, separate body/status/key
  fragments, narrow/wide body rendering, and finite one-shot joining.
- Extracted lookup coordinator and reduced interactive session owner.
- Public PTY, semantic renderer, state, routing, and test-fake coverage.
- Synthetic packaging benchmark compatibility.

## Checkpoint history

A host restart was requested during implementation, so recoverable WIP was
preserved in:

```text
862016fb7dd492179761a08100abb48888454003
chore(dashboard): checkpoint height-aware terminal WIP
```

The final implementation is committed separately with the planned subject:

```text
fix(dashboard): make interactive layout height aware
```

## Self-review and concerns

- Reviewed every Task 2 diff after the final test repairs.
- The semantic focus lookup relies on the existing unique cursor-role
  invariant; the existing controller and renderer tests cover that invariant.
- The one-shot renderer uses a fixed 60-row semantic viewport only to select
  the non-compact presentation; it does not emit terminal control sequences or
  own interactive geometry.
- No functional concern remains. The installed-console benchmark was not run
  because this task explicitly prohibits using or modifying the installed
  reporter; focused renderer evidence is recorded above.

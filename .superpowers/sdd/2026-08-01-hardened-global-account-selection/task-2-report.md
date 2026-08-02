# Task 2 WIP Restart Checkpoint

Status: paused for the host restart requested on 2026-08-02. This is not a
Task 2 completion report, and the final planned commit subject has not been
used.

## Scope and baseline

- Worktree:
  `/home/sabossedgh/dev/.worktrees/sidekick-usages-hardened-global-selection`
- Branch: `feat/hardened-global-account-selection`
- Task 2 base: `1ecb80617d9dabf1b0ff67f198f4266a31bd5da3`
- Task 1 saved-only `DashboardSnapshot` and `DashboardCursor` contracts remain
  the consumed contracts. No external pseudo-row compatibility was added.
- No live Sidekick or provider state was accessed. The public PTY matrix used
  isolated temporary `HOME` and XDG roots. The installed reporter was not
  changed.

## Completed WIP changes

### Test-first coverage

- Added the required five-size public-route PTY matrix for `(52, 24)`,
  `(79, 40)`, `(80, 48)`, `(100, 49)`, and `(120, 60)`.
- Extended the existing interactive journey to resize the saved Codex focus
  from `(100, 49)` to `(52, 24)` and require the same selected label plus the
  visible key footer.
- Added the planned semantic too-short-layout test scaffold and finite,
  escape-free one-shot assertions. It intentionally references the remaining
  `TerminalDimensions`, `TERMINAL_TOO_SHORT`, and
  `render_dashboard_layout()` implementation.

### Interactive terminal ownership

- Removed cached dashboard painting from `bootstrap.main()`.
- Removed `present_cached_dashboard()`, frame cursor-up, and failed-replace
  cursor-down ownership by deleting `cli/dashboard/launch.py`.
- Interactive routing now immediately executes the isolated dashboard process
  image. The dashboard entrypoint remains the cached-state loader before the
  first prompt-toolkit render.

### Lookup cohesion boundary

- Added `cli/dashboard/lookup.py` with
  `DashboardLookupCoordinator.start()`, `close()`, and `apply()`.
- Moved lookup worker execution, recoverable retry, diagnostic recording,
  snapshot retry/read handling, and immutable account-outcome overlay state
  out of `InteractiveDashboardSession`.
- Kept navigation, action submission, startup reconciliation, footer state,
  and lookup-result presentation in `InteractiveDashboardSession`.
- Updated the session fake to import the lookup thread owner name from its new
  cohesive module.
- Current module sizes are 732 lines for `session.py` and 335 lines for
  `lookup.py`, both below the repository cohesion threshold.

## Preserved RED evidence

Command:

```bash
uv run pytest tests/dashboard/test_pty.py -q
```

Result before implementation changes:

```text
collected 7 items
tests/dashboard/test_pty.py FFFFFF. [100%]
6 failed, 1 passed in 10.38s
```

All five public-route matrix cases failed because complete captured output had
three occurrences of `sidekick usages` instead of one. The existing journey
failed after the `(100, 49)` to `(52, 24)` resize because the stable saved
Codex row was not visible/selected. This is the expected evidence for duplicate
bootstrap/prompt-toolkit painting plus one full-frame Window clipping at short
height.

An earlier invalid fixture attempt exited the public child with status 1
because the isolated WSL fixture omitted `WSL_DISTRO_NAME`. The fixture was
corrected to the synthetic value `Ubuntu`, then the exact RED command above
produced the intended behavioral failures rather than a setup error.

## Current GREEN evidence

Lookup/session behavior after extraction:

```bash
uv run pytest tests/dashboard/test_state.py -q
```

```text
collected 3 items
tests/dashboard/test_state.py ... [100%]
3 passed in 0.61s
```

Focused static checks after extraction:

```bash
uv run ruff check src/sidekick_usages/cli/dashboard/lookup.py \
  src/sidekick_usages/cli/dashboard/session.py
uv run ty check src/sidekick_usages/cli/dashboard/lookup.py \
  src/sidekick_usages/cli/dashboard/session.py
```

Both commands reported `All checks passed!`.

Restart checkpoint hygiene:

```bash
git diff --check
```

Exited with status 0 and no output.

The process check found no remaining synthetic lookup, PTY child, dashboard,
or pytest process. It matched only the short-lived `ps`/`rg` diagnostic itself.

## Exact remaining implementation

1. Add `TerminalDimensions` and `DashboardRenderLayout` in the semantic render
   model boundary with explicit validation.
2. Extend `brand_layout(width, *, compact=False)` and adapt `brand_lines()` so
   short-height rendering uses the canonical one-line brand plus divider.
3. Implement `render_dashboard_layout()` to return separate masthead, body,
   status, and key fragments, plus the focused saved-row body line. Add the
   typed `TERMINAL_TOO_SHORT` status below the supported 24-row height.
4. Refactor the narrow and wide dashboard builders so account/panel content is
   a scrollable body rather than a frame containing masthead and footer.
5. Keep `render_dashboard()` as the finite one-shot join of those same semantic
   fragments with no cursor/alternate-screen control sequences.
6. Replace the one full-frame prompt-toolkit Window with one `HSplit`: fixed
   preferred-height masthead, status, and keys around one filling body Window.
   Read rows and columns from `get_app().output.get_size()` during render and
   expose the hidden focused-row cursor through
   `FormattedTextControl(get_cursor_position=...)`.
7. Update `tests/dashboard/test_routing.py` to remove the deleted cached-paint
   contract/import and assert immediate interactive process replacement plus
   two-dimensional terminal acquisition. Do not add a compatibility re-export.
8. Re-run the PTY proof. The matrix capture currently counts the raw PTY
   transcript, so after bootstrap painting is removed it may still count more
   than one prompt-toolkit invalidation. If so, make `run_dashboard_screen()`
   represent the logical visible screen/scrollback after cursor rewrites; do
   not weaken the one-masthead assertion or ignore duplicate cached paint.
9. Run the exact focused test and Ruff commands from the Task 2 brief, then run
   focused `ty`, architecture, and diff checks proportional to the changed
   ownership boundary.
10. Self-review every changed file, replace this WIP report with the complete
    RED/GREEN/verification report, and create the final planned commit with
    subject `fix(dashboard): make interactive layout height aware` only after
    all Task 2 requirements are green.

## Files included in the WIP checkpoint

- Deleted: `src/sidekick_usages/cli/dashboard/launch.py`
- Added: `src/sidekick_usages/cli/dashboard/lookup.py`
- Modified: `src/sidekick_usages/cli/dashboard/session.py`
- Modified: `src/sidekick_usages/cli/runtime/bootstrap.py`
- Modified: `tests/dashboard/test_pty.py`
- Modified: `tests/fakes/dashboard/session/snapshots.py`
- Modified: `tests/usage/test_dashboard_render.py`
- Added/updated: this checkpoint report

## Resume commands

```bash
cd /home/sabossedgh/dev/.worktrees/sidekick-usages-hardened-global-selection
git status --short --branch
git log -2 --oneline
sed -n '1,260p' \
  .superpowers/sdd/2026-08-01-hardened-global-account-selection/task-2-report.md
uv run pytest tests/dashboard/test_state.py -q
```

After the remaining implementation:

```bash
uv run pytest tests/dashboard/test_pty.py \
  tests/usage/test_dashboard_render.py tests/dashboard/test_state.py -q
uv run ruff check src/sidekick_usages/cli/runtime/bootstrap.py \
  src/sidekick_usages/cli/dashboard \
  src/sidekick_usages/usage/presentation/dashboard \
  src/sidekick_usages/branding tests/dashboard tests/usage
uv run ty check src/ tests/ packaging/
uv run python packaging/check_architecture.py
git diff --check
```

## Concerns at pause

- Task 2 is deliberately incomplete: `test_dashboard_render.py` imports the
  not-yet-created semantic layout API, and `test_routing.py` still imports the
  deleted `launch.py`. Do not run or report the combined Task 2 suite as green
  until those remaining steps are implemented.
- The WIP lookup extraction passed the existing state suite and focused static
  checks, but must still be exercised through the PTY and full focused Task 2
  suite after the HSplit integration.
- The raw-transcript screen helper concern described in remaining step 8 must
  be resolved by accurate terminal-screen semantics, not by loosening the
  required assertion.

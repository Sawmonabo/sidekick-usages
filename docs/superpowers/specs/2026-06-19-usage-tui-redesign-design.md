# Design Spec — Usage TUI Redesign ("Framed Panels" heatmap)

- **Status:** **Spec approved & signed off 2026-06-19** → proceeding to
  `writing-plans`. The generic manual plan override (§9.2) was folded into this
  branch per sign-off.
- **Date:** 2026-06-19
- **Masthead refinement:** **Approved 2026-07-09** — add the robot logo,
  remove the global account/provider summary, and show each provider's account
  count in its panel title. Existing failure rows remain the only failure-status
  treatment.
- **Branch:** `feat/usage-tui-redesign`
- **Visual reference (not shipped):** scratchpad `variants.py` → `framed.svg`,
  previewed faithfully via Rich `export_svg` in the browser.

---

## 1. Context & problem

The current usage display (`render.py:usage_report`) renders **one block per
account**: a header line plus a borderless table of braille progress bars
(`⣿⣀`), percent, and a verbose reset string
(`↻ Wed Jun 18, 03:50 PM (in 3h 50m)`).

Problems the user called out:
- The braille bars are **hard to read/scan**.
- There is **no clear separation between Claude and Codex** accounts.
- It is not "elegant."

**Goal (non-negotiable):** optimize for **both scannability and elegance**.
Runtime rendering preserves configured account names and plans verbatim and
must work in fixed-cell, truecolor terminals.

---

## 2. Chosen design — "Framed Panels"

A heatmap matrix where a cell's **background color encodes utilization**,
organized into two clearly separated provider panels.

```
      o
     .-.
  .--┴-┴--.    sidekick usages
  | O   O |   >> A multi-account usage dashboard for Claude Code and Codex CLI.
  | ||||| |   >> Limits + resets + account status, one terminal.
  '--___--'
─────────────────────────────────────────────────────────────────────────────────────

╭─ CLAUDE · 3 accounts ──────────────────────────────────────────────────╮
│                                          5h     7d                     │
│ ● short.account@example.test      max   [94%]  [61%]                   │
│                                         3h 50m 1d 15h                  │
│ ● long.account.name@example.test  team  [12%]  [73%]                   │
│ ...                                                                    │
╰──────────────────────────────  424M output · since Dec 28 ─────────────╯

╭─ CODEX · 2 accounts ───────────────────────────────────────────────────╮
│                          5h     7d    Spark  5h     7d                 │
│ ● codex@example.test pro [8%]  [45%]         [·]   [·]                 │
│ ...                                                                    │
╰──────────────────────────────  212M output · since Mar 30 ─────────────╯

 <40   40-69   70-89   ≥90      dim = resets in
```

**Structure:**
- **Robot masthead** (outside panels): the six-line robot logo, `sidekick
  usages` title, two lines of stable product copy, and a dim horizontal rule.
  There is no global `N accounts · M providers` summary.
- **One rounded panel per provider**, border in the provider color (Claude =
  magenta, Codex = cyan):
  - **Panel title** (left): provider name plus the number of successful and
    failed accounts represented in that panel, with correct singular/plural
    wording (`1 account`, `2 accounts`). No alert or `needs attention` suffix
    is added; existing failure rows retain that responsibility.
  - **Panel footer/subtitle** (right): lifetime **output** tokens across all
    accounts + `· since <date>`, in a single faint tone (see §6).
  - **Body:** a borderless matrix table.
- **Per account = a 2-row group** with a blank separator row between
  accounts:
  - **Row 1 (utilization):** `●` provider dot followed by one space · account
    label · plan tag · then one **heat tile per window**.
  - **Row 2 (reset):** dim compact countdown beneath each tile (glyph-free).
- **Legend** (bottom, outside panels): four heat-band swatches +
  `dim = resets in`.

**Column model (per panel):**
- Fixed left block: dot (width 1) · name (width = longest label) · plan
  (width 4).
- **Primary window group** columns `5h` and `7d` — these sit at a
  **consistent x-position across both providers** so the eye aligns them.
- **Additional window groups** (e.g. Codex "Spark") render as a labeled
  column-block to the right of the primary group. See §4.

---

## 3. Heat encoding (exact)

Band by utilization percent (lower bound inclusive). Foreground/background
are truecolor hex:

| Band | Range | fg | bg |
|------|-------|----|----|
| red | ≥ 90 | `#ffe6e6` | `#b03030` |
| amber | 70–89 | `#fff4e0` | `#9c6f12` |
| blue | 40–69 | `#e2fbff` | `#1b6a87` |
| green | 1–39 | `#dfffe9` | `#1d5e35` |
| zero | 0 (any) | faint grey fg (`grey39`), **no fill** — centered `·` | — |

- The **thresholds match the existing `_utilization_color` bands** (red ≥ 90 /
  yellow ≥ 70 / cyan ≥ 40 / green) — only the *expression* changes from
  foreground-only color names to filled truecolor tiles.
- A utilization cell shows `NN%` centered in the tile; the whole fixed-width
  cell carries the background so it renders as one solid block. Rich emits one
  `<rect>` per styled run, with no seams.
- **Zero vs inactive:** a `0%` cell **always** renders the faint `·` (no fill),
  regardless of whether the window is currently active. The **reset countdown
  beneath it is independent** — it appears for any window carrying a
  `resets_at`. So an *active* 0% window shows `·` over a live countdown (see
  the reserved 30-character fixture row), while a *truly inactive* window
  (`utilization == 0` **and** `resets_at is None`, per
  `report.py:is_active`) shows `·` with a blank countdown row.
- **Legend swatch order:** `<40` (green) · `40-69` (blue) · `70-89` (amber) ·
  `≥90` (red).

**Truecolor fallback:** Rich auto-downsamples truecolor on 256/16-color
terminals. Implementation must verify the four bands stay visually distinct
after downsampling and degrade gracefully when color is unavailable
(`NO_COLOR`, non-tty). The worst-case fallback is the foreground-only color
scheme.

---

## 4. Window classification

Window names arrive as free strings on `UsageWindow.name` (e.g. `"5h"`, `"7d"`,
`"7d Opus"`, and Codex's secondary "Spark" windows).

**Rule (no hardcoded provider tables):**
1. In each window name, find the **length token** — a run matching `\d+[hd]`
   (e.g. `5h`, `7d`) anywhere in the string. That is the window's **length**.
2. The remaining text, trimmed, is the **group label** (`""` = the provider's
   main limit; named groups include `"Opus"` and `"Spark"`).

**Layout consequence:**
- The **main group** (`""`) supplies the aligned primary `5h` / `7d` columns.
- Each **named group** becomes its own labeled column-block (header = the group
  label) appended to the right, preserving length order within it.
- The set of groups is **driven by live data per provider**, not assumed. The
  approved mockup shows the common case (Claude → main only; Codex → main +
  `Spark`). If Claude returns an `Opus` group, it renders as an extra block
  automatically.

> Open implementation detail for `writing-plans`: confirm exact live
> window-name strings for both providers and the desired group ordering and
> labels.

---

## 5. Reset countdown format

- **Compact relative only:** `Xm`, `Xh Ym`, `Xd Yh` — space-separated
  (`3h 50m`, `1d 15h`, `5d 7h`), dim grey, **no `↻` glyph**.
- This replaces the verbose `↻ <local timestamp> (in …)` string **within the
  matrix**. The absolute timestamp added width and clutter; the countdown is
  what users scan. The verbose form can remain available in a future detail
  view.
- Reuse the existing relative-time bucketing in `_format_reset` (seconds → m /
  h m / d h); drop the timestamp and glyph.

---

## 6. Lifetime output tokens (per provider, all accounts) — NEW data feature

- **What:** cumulative **output** tokens, **summed across all accounts of a
  provider**, shown faint in each panel footer:
  `424M output · since Dec 28`.
- **Why output (not input/total):** it is the only measure **comparable across
  providers**. Claude reports cache-read separately; Codex folds cached tokens
  into `input_tokens`. Input- and total-based figures are therefore
  apples-to-oranges; output is clean. Total throughput is about 48 B each and
  cache-dominated, which would be misleading.
- **Leak-free:** aggregate **only**. Never per-account — no per-account
  attribution is recoverable from local logs because neither provider stamps
  an account into session logs. See §9.
- **Sources (read-only, local):**
  - **Claude:** `~/.claude/stats-cache.json` → `modelUsage` per-model lifetime
    output. This is machine-wide, covers all accounts, and updates lazily.
    `since` approximates the earliest date in `dailyModelTokens` (about Dec
    28). Observed total: about **424M**.
  - **Codex:** sum output across `~/.codex/sessions/**/rollout-*.jsonl` using
    cumulative `payload.info.total_token_usage.output_tokens` or
    `last_token_usage` deltas. `since` is the earliest rollout date (about Mar
    30). Observed total: about **212M**.
- **Honest caveat #1 (retention):** "lifetime" is bounded by **local log
  retention**, not true account lifetime. `since <date>` makes that explicit.
- **Honest caveat #2 (Claude figure scope):** `stats-cache.json` is
  **machine-wide**. It sums *all* Claude Code output on this machine, including
  accounts not managed by sidekick. The Claude panel's number is therefore
  "this machine's Claude Code output," which may **exceed the sum of the
  accounts shown in the panel**. It remains aggregate and output-only, but the
  footer must not imply "sum of exactly these rows." Codex is summed from the
  rollout logs on disk, so it reflects the sessions present there.
- **Computed at runtime** (numbers grow). **Perf flag:** Codex summing touches
  about 1500 files and likely needs caching. Deferred to `writing-plans`.

---

## 7. Architecture / touchpoints (orientation, not step-by-step)

- **`report.py`** (`UsageWindow`, `UsageReport`): structurally unchanged.
  Optionally add a small pure helper to classify `name → (length, group)`
  (§4), or keep it in the renderer.
- **`render.py`:**
  - Add heat helpers: `band(pct) → (fg,bg)`, utilization tile cell, and compact
    reset cell.
  - **New provider-grouped entry point.** Today
    `usage_report(acct, report)` renders a single account. The new top-level
    renderer takes **all `(Account, UsageReport)` pairs**, groups by provider,
    and emits: robot masthead → one `Panel` per provider (provider-local
    account count + matrix + footer) → legend. Per-account
    `usage_report`/`account_header` may be retained for error/empty blocks.
  - Reset formatting → compact relative (§5).
- **NEW lifetime-tokens code** (module or functions): read `stats-cache.json`
  for Claude and sum Codex rollouts; return per-provider
  `(output_total, since_date)`. Caching is considered in §6.
- **Caller (`cli` / main entry):** switch from the per-account print loop to
  the new grouped renderer and thread lifetime lookups into panel footers.

### 7.1 Shared application branding refinement (2026-07-09)

The masthead is an application component, not usage-renderer-owned text:

- `branding.py` is the single source of truth for the six robot rows, product
  name and copy, provider colors, styles, spacing, and responsive layouts. It
  depends only on Rich and the standard library, so rendering it cannot load
  accounts, credentials, providers, or network clients.
- `brand_header(width, section=...)` selects the full 79-cell masthead, the
  robot-and-title narrow form, or a title-only fallback. All forms are composed
  from the same canonical constants.
- `update_status_line()` provides the compact status treatment used by
  `check-update`.
- `cli_help.py` adapts Typer command and group help once. Root, nested-group,
  and leaf help prepend the shared header before `Usage:` without initializing
  application state. The heartbeat label-fallback group retains its special
  parsing behavior by extending the branded group class.
- `render.py`, `doctor.py`, `heartbeat/render.py`, and `cli.py` consume these
  public renderers. None repeats the logo or product copy.

Branding is deliberately attached at presentation boundaries rather than a
global before-command hook:

| Surface | Treatment |
| --- | --- |
| Default/check and no-account check | Responsive masthead |
| Root, group, and command help | Responsive masthead before `Usage:` |
| Doctor, list, heartbeat status, daemon status | Masthead plus section label |
| Successful `check-update` | Compact update-status line |
| JSON, quiet/scheduled, version, mutations, early errors | No branding |

This keeps machine-readable and automation-oriented contracts byte-compatible
while giving human-facing overview and status screens a consistent identity.

---

## 8. Constraints

- **Width — hard floor 80 columns.** The CLI renders at the *actual* terminal
  width (Rich `Panel` `expand=True`), so 80 (the universal default) is the
  worst case the layout must not break. The following values were measured
  with scratchpad `confirm.py` and `expand=True` at width 80, matching the
  actual CLI condition:
  - **Claude** panel natural width = **57 cols** → fits with room.
  - **Codex** panel is the binding case: it carries the extra **"Spark"**
    column-block, so its inner table is **78 columns**. With Rich's default
    panel `padding=(0,1)`, the panel needs **82**. At an 80-column terminal,
    the 76-column inner area cannot hold 78, so Rich squeezes the Spark block
    (`Spark` → `Spa…`) and **wraps reset countdowns** onto extra physical
    lines. **Broken.**
  - **Fix (verified clean):** render the panels with **zero horizontal
    padding** (`padding=(0,0)`) → Codex = **exactly 80**. The 30-character
    fixture (`long.account.name@example.test`) renders intact on one line,
    `Spark` stays intact, and nothing wraps. A full-width panel at exactly the
    terminal width is Rich's normal behavior (VT100 deferred-wrap), not an
    off-by-one risk. *(Elegance alternative for `writing-plans`: instead of
    zero panel padding — which sits the dot flush to the border — reclaim the
    same 2 columns from inter-group spacing to keep a 1-column inner margin.
    Both fit 80; pick in implementation.)*
  - **Why it's tight:** the **shared 30-column name width** is the dominant
    cost and keeps `5h`/`7d` aligned across providers. Account names are shown
    **verbatim — no elision at ≥80**.
  - **Below 80:** the layout degrades by squeezing the name column or
    truncating Spark. Implementation must detect `width < 80` and degrade
    deliberately by eliding the name or using the legacy per-account view.
    It must **never silently wrap**.

> **Update (2026-06-19):** the implementation chose the "roomier look"
> alternative noted above. Breathing-room padding `(1,2)` (one vertical row,
> zero width cost, and two-column horizontal side margins) was approved during
> showcase refinement, and the account-name column is shown verbatim. The
> 30-character fixture `long.account.name@example.test`, combined with those
> side margins, makes the Codex panel the binding case at **85 columns**, not
> 80. The renderer already degrades to the legacy stacked view when
> `width < required`; only the threshold moved from 80 to **85** (behavior
> unchanged). The original 80-column target, derived with `padding=(0,0)`, was
> traded for the roomier look per explicit user decision.

**Masthead update (2026-07-09):** The longest robot/product-copy line is
**79 cells**. It therefore fits inside the existing 85-column worst-case
overview. For smaller account sets, 79 becomes the framed view's minimum
width; narrower terminals continue to use the existing legacy stacked view
instead of wrapping or cropping the logo.

- **Truecolor:** required for heat tiles; graceful downsample/fallback per §3.
- **Runtime data:** preserve configured account labels and plan tags verbatim.
  Suppress the plan tag only when empty or `unknown`, per `_account_tag`.

---

## 9. Out of scope / follow-ups

1. **Per-account token spend** (5h / 7d / lifetime): **excluded.** It is not
   safely recoverable without cross-account leakage. Codex *current-window*
   per-account attribution via a `resets_at` fingerprint join is technically
   possible, but Claude has no equivalent. This remains a distinct future
   feature, not part of this redesign.
2. ~~Plan correction (originally proposed as a separate follow-up)~~ → **In
   scope for this branch (sign-off decision 2026-06-19).** The usage API cannot
   always infer a plan for inference-only credentials. The delivered solution
   is the generic, explicit `set-plan` command; no account-specific mapping or
   automatic active-login mutation was added. The command remained separate
   from rendering work so it could be reviewed and reverted independently.
3. **Lifetime-token perf:** settle the Codex rollout caching strategy in
   `writing-plans`.

---

## 10. Validation

- Faithful preview via Rich `export_svg` → browser (used throughout design);
  reuse it to verify the implementation against the approved mockup.
- **Width regression guard (required):** render the reserved worst-case fixture
  (Codex + Spark + 30-character name) at `Console(width=85)`. Assert no
  physical line passes 85 cells and the longest name remains intact on one
  row. This permanently covers the exact design-time failure. The floor moved
  from 80 to 85 as recorded in §8.
- Use focused behavioral assertions for the 85-column boundary, stable copy
  and essential styles, provider grouping, heat-band thresholds, window
  classification, and zero-versus-active reset behavior. Do not snapshot
  whole screens.

---

## Sign-off — RESOLVED (2026-06-19)

- **Spec approved.** Proceeding to `writing-plans`.
- **Plan correction:** the generic manual `set-plan` capability was folded
  into this branch. No account-specific mapping or live-login mutation was
  shipped. Tracked as §9.2 and committed separately from rendering work.
- **Masthead refinement approved 2026-07-09.** Use the robot masthead and
  provider-local account counts exactly as shown in §2. Do not add separate
  alert-count or `needs attention` wording to panel titles.

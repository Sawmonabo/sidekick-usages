# Design Spec — Usage TUI Redesign ("Framed Panels" heatmap)

- **Status:** **Spec approved & signed off 2026-06-19** → proceeding to `writing-plans`. Plan-detection fix (§9.2) folded **into this branch** per sign-off.
- **Date:** 2026-06-19
- **Masthead refinement:** **Approved 2026-07-09** — add the robot logo,
  remove the global account/provider summary, and show each provider's account
  count in its panel title. Existing failure rows remain the only failure-status
  treatment.
- **Branch:** `feat/usage-tui-redesign`
- **Visual reference (not shipped):** scratchpad `variants.py` → `framed.svg`, previewed faithfully via Rich `export_svg` in the browser.

---

## 1. Context & problem

The current usage display (`render.py:usage_report`) renders **one block per account**: a header line plus a borderless table of braille progress bars (`⣿⣀`), percent, and a verbose reset string (`↻ Wed Jun 18, 03:50 PM (in 3h 50m)`).

Problems the user called out:
- The braille bars are **hard to read/scan**.
- There is **no clear separation between Claude and Codex** accounts.
- It is not "elegant."

**Goal (non-negotiable):** optimize for **both scannability and elegance**; use the user's **real configured account names/plans** verbatim; **must render correctly in the user's actual terminal** (fixed-cell, truecolor).

---

## 2. Chosen design — "Framed Panels"

A heatmap matrix where a cell's **background color encodes utilization**, organized into two clearly separated provider panels.

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
│ ● SAbossedgh@fortressinfosec      max   [94%]  [61%]                   │
│                                         3h 50m 1d 15h                  │
│ ● SAbossedgh@fortressinfosec@org  team  [12%]  [73%]                   │
│ ...                                                                    │
╰──────────────────────────────  424M output · since Dec 28 ─────────────╯

╭─ CODEX · 2 accounts ───────────────────────────────────────────────────╮
│                          5h     7d    Spark  5h     7d                 │
│ ● a.sawmon@ymail.com pro [8%]  [45%]         [·]   [·]                 │
│ ...                                                                    │
╰──────────────────────────────  212M output · since Mar 30 ─────────────╯

 <40   40-69   70-89   ≥90      dim = resets in
```

**Structure:**
- **Robot masthead** (outside panels): the six-line robot logo, `sidekick
  usages` title, two lines of stable product copy, and a dim horizontal rule.
  There is no global `N accounts · M providers` summary.
- **One rounded panel per provider**, border in the provider color (Claude = magenta, Codex = cyan):
  - **Panel title** (left): provider name plus the number of successful and
    failed accounts represented in that panel, with correct singular/plural
    wording (`1 account`, `2 accounts`). No alert or `needs attention` suffix is
    added; existing failure rows retain that responsibility.
  - **Panel footer/subtitle** (right): lifetime **output** tokens across all accounts + `· since <date>`, in a single faint tone (see §6).
  - **Body:** a borderless matrix table.
- **Per account = a 2-row group** with a blank separator row between accounts:
  - **Row 1 (utilization):** `● ` provider dot · account label · plan tag · then one **heat tile per window**.
  - **Row 2 (reset):** dim compact countdown beneath each tile (glyph-free).
- **Legend** (bottom, outside panels): four heat-band swatches + `dim = resets in`.

**Column model (per panel):**
- Fixed left block: dot (width 1) · name (width = longest label) · plan (width 4).
- **Primary window group** columns `5h` and `7d` — these sit at a **consistent x-position across both providers** so the eye aligns them.
- **Additional window groups** (e.g. Codex "Spark") render as a labeled column-block to the right of the primary group. See §4.

---

## 3. Heat encoding (exact)

Band by utilization percent (lower bound inclusive). Foreground/background are truecolor hex:

| Band | Range | fg | bg |
|------|-------|----|----|
| red | ≥ 90 | `#ffe6e6` | `#b03030` |
| amber | 70–89 | `#fff4e0` | `#9c6f12` |
| blue | 40–69 | `#e2fbff` | `#1b6a87` |
| green | 1–39 | `#dfffe9` | `#1d5e35` |
| zero | 0 (any) | faint grey fg (`grey39`), **no fill** — centered `·` | — |

- The **thresholds match the existing `_utilization_color` bands** (red ≥ 90 / yellow ≥ 70 / cyan ≥ 40 / green) — only the *expression* changes from fg-only color names to filled truecolor tiles.
- A utilization cell shows `NN%` centered in the tile; the whole fixed-width cell carries the bg so it renders as one solid block (verified: Rich emits one `<rect>` per styled run — no seams).
- **Zero vs inactive:** a `0%` cell **always** renders the faint `·` (no fill), regardless of whether the window is currently active. The **reset countdown beneath it is independent** — it appears for any window carrying a `resets_at`. So an *active* 0% window shows `·` over a live countdown (see the Codex `sabossedgh@…` row in the verified width render), while a *truly inactive* window (`utilization == 0` **and** `resets_at is None`, per `report.py:is_active`) shows `·` with a blank countdown row.
- **Legend swatch order:** `<40` (green) · `40-69` (blue) · `70-89` (amber) · `≥90` (red).

**Truecolor fallback:** Rich auto-downsamples truecolor on 256/16-color terminals. Implementation must verify the four bands stay visually distinct after downsample, and degrade gracefully (worst case: fall back to the fg-only color-name scheme) when color is unavailable (`NO_COLOR`, non-tty).

---

## 4. Window classification

Window names arrive as free strings on `UsageWindow.name` (e.g. `"5h"`, `"7d"`, `"7d Opus"`, Codex's secondary "Spark" windows).

**Rule (no hardcoded provider tables):**
1. In each window name, find the **length token** — a run matching `\d+[hd]` (e.g. `5h`, `7d`) anywhere in the string. That is the window's **length**.
2. The remaining text, trimmed, is the **group label** (`""` = the provider's main limit; named groups such as `"Opus"` or `"Spark"`).

**Layout consequence:**
- The **main group** (`""`) supplies the aligned primary `5h` / `7d` columns.
- Each **named group** becomes its own labeled column-block (header = the group label) appended to the right, preserving length order within it.
- The set of groups is **driven by live data per provider**, not assumed. The approved mockup shows the common case (Claude → main only; Codex → main + `Spark`). If Claude returns an `Opus` group, it renders as an extra block automatically.

> Open implementation detail for `writing-plans`: confirm exact live window-name strings for both providers and the desired group ordering/labels.

---

## 5. Reset countdown format

- **Compact relative only:** `Xm`, `Xh Ym`, `Xd Yh` — space-separated (`3h 50m`, `1d 15h`, `5d 7h`), dim grey, **no `↻` glyph**.
- This replaces the verbose `↻ <local timestamp> (in …)` string **within the matrix**. (Rationale: the absolute local timestamp added width and clutter; the countdown is what users scan. The verbose form can remain available elsewhere if a detail view is ever added.)
- Reuse the existing relative-time bucketing in `_format_reset` (seconds → m / h m / d h); drop the timestamp + glyph.

---

## 6. Lifetime output tokens (per provider, all accounts) — NEW data feature

- **What:** cumulative **output** tokens, **summed across all accounts of a provider**, shown faint in each panel footer: `424M output · since Dec 28`.
- **Why output (not input/total):** it is the only measure **comparable across providers**. Claude reports cache-read separately; Codex folds cached tokens into `input_tokens`. So input- and total-based figures are apples-to-oranges; output is clean. (Total throughput is ~48 B each and cache-dominated — misleading.)
- **Leak-free:** aggregate **only**. Never per-account — no per-account attribution is recoverable from local logs (neither provider stamps an account into session logs; established during investigation). See §9.
- **Sources (read-only, local):**
  - **Claude:** `~/.claude/stats-cache.json` → `modelUsage` per-model lifetime output (machine-wide, all accounts; updates lazily). `since` ≈ earliest date in `dailyModelTokens` (≈ Dec 28). Observed ≈ **424M**.
  - **Codex:** sum output across `~/.codex/sessions/**/rollout-*.jsonl` (`payload.info.total_token_usage.output_tokens` cumulative, or `last_token_usage` deltas). `since` = earliest rollout date (≈ Mar 30). Observed ≈ **212M**.
- **Honest caveat #1 (retention):** "lifetime" is bounded by **local log retention**, not true account lifetime — `since <date>` makes that explicit.
- **Honest caveat #2 (Claude figure scope):** `stats-cache.json` is **machine-wide** — it sums *all* Claude Code output on this machine, including any account not managed by sidekick. So the Claude panel's number is "this machine's Claude Code output," which may **exceed the sum of the accounts shown in the panel**. Still aggregate + output-only (no per-account leak), but the footer wording must not imply "sum of exactly these rows." (Codex is summed from the rollout logs on disk, so it reflects the sessions actually present.)
- **Computed at runtime** (numbers grow). **Perf flag:** Codex summing touches ~1500+ files — likely needs caching/memoization. Deferred to `writing-plans`.

---

## 7. Architecture / touchpoints (orientation, not step-by-step)

- **`report.py`** (`UsageWindow`, `UsageReport`): structurally unchanged. Optionally add a small pure helper to classify `name → (length, group)` (§4), or keep it in the renderer.
- **`render.py`:**
  - Add heat helpers: `band(pct) → (fg,bg)`, utilization tile cell, compact reset cell.
  - **New provider-grouped entry point.** Today
    `usage_report(acct, report)` renders a single account. The new top-level
    renderer takes **all `(Account, UsageReport)` pairs**, groups by provider,
    and emits: robot masthead → one `Panel` per provider (provider-local account
    count + matrix + footer) → legend. Per-account
    `usage_report`/`account_header` may be retained for error/empty blocks.
  - Reset formatting → compact relative (§5).
- **NEW lifetime-tokens code** (module or functions): read `stats-cache.json` (Claude) and sum rollouts (Codex); return per-provider `(output_total, since_date)`. Caching considered (§6).
- **Caller (`cli` / main entry):** switch from the per-account print loop to the new grouped renderer; thread the lifetime-token lookups into the panel footers.

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
| Successful `check-update` | Compact brand line |
| JSON, quiet/scheduled, version, mutations, early errors | No branding |

This keeps machine-readable and automation-oriented contracts byte-compatible
while giving human-facing overview and status screens a consistent identity.

---

## 8. Constraints

- **Width — hard floor 80 columns.** The CLI renders at the *actual* terminal width (Rich `Panel` `expand=True`), so 80 (the universal default) is the worst case the layout must not break. **Empirically measured** (scratchpad `confirm.py`, `expand=True` @ width 80 — the true CLI condition):
  - **Claude** panel natural width = **57 cols** → fits with room.
  - **Codex** panel is the binding case: it carries the extra **"Spark"** column-block, so its inner table is **78 cols**. With Rich's default panel `padding=(0,1)` the panel needs **82** → at an 80-col terminal the inner area (76) can't hold 78, so Rich squeezes the Spark block (`Spark` → `Spa…`) and **wraps the reset countdowns** onto extra physical lines. **Broken.**
  - **Fix (verified clean):** render the panels with **zero horizontal padding** (`padding=(0,0)`) → Codex = **exactly 80**, the 30-char name (`sabossedgh@fortressinfosec.com`) renders intact on one line, `Spark` intact, no wrapping. A full-width panel at exactly the terminal width is Rich's normal behavior (VT100 deferred-wrap), not an off-by-one risk. *(Elegance alternative for `writing-plans`: instead of zero panel padding — which sits the dot flush to the border — reclaim the same 2 cols from inter-group spacing to keep a 1-col inner margin. Both fit 80; pick in implementation.)*
  - **Why it's tight:** the **shared 30-col name width** (required so the `5h`/`7d` columns align across both providers) is the dominant cost. Account names are shown **verbatim — no elision at ≥80**.
  - **Below 80:** the layout degrades (name column squeezed / Spark truncated). Implementation must detect `width < 80` and degrade deliberately — elide the name column or fall back to the legacy per-account view — **never silently wrap**.

> **Update (2026-06-19):** the implementation chose the "roomier look" alternative noted above — breathing-room padding `(1,2)` (1 row vertical, zero width cost; 2 cols horizontal side margins) was approved during showcase refinement, and the model name column is shown verbatim. The 30-char Codex name `SAbossedgh@fortressinfosec@org` combined with those side margins makes the Codex panel the binding case at **85 cols**, not 80. The renderer already degrades to the legacy stacked view when `width < required`; only the threshold moved from 80 to **85** (behavior unchanged). The original 80-col target, derived with `padding=(0,0)`, was traded for the roomier look per explicit user decision.

**Masthead update (2026-07-09):** The longest robot/product-copy line is
**79 cells**. It therefore fits inside the existing 85-column worst-case
overview. For smaller account sets, 79 becomes the framed view's minimum
width; narrower terminals continue to use the existing legacy stacked view
instead of wrapping or cropping the logo.

- **Truecolor:** required for heat tiles; graceful downsample/fallback per §3.
- **Real data:** the user's actual configured account labels and plan tags, verbatim (plan tag suppressed only when empty/`unknown`, per existing `_account_tag`).

---

## 9. Out of scope / follow-ups

1. **Per-account token spend** (5h / 7d / lifetime): **excluded.** Not safely recoverable without cross-account leakage. (Codex *current-window* per-account attribution via a `resets_at` fingerprint join is technically possible; Claude has no equivalent. Deferred as a distinct future feature, not part of this redesign.)
2. ~~Plan-detection bug (was proposed as a separate follow-up)~~ → **Now IN SCOPE for this branch (sign-off decision 2026-06-19).** Account `SAbossedgh@fortressinfosec` is stored as plan `unknown` but is actually `max`; with the existing `_account_tag` suppression it would otherwise render a **blank** plan tag, marring the very showcase we're polishing. This branch will fix `unknown → max` so the panel matches the approved mockup. **Open for `writing-plans`:** investigate *why* it reads `unknown` (a missing plan-mapping entry vs. genuinely undetectable from available account data) and size the fix; keep the detection change in **separate commits** from the rendering change so it can be reviewed/reverted independently.
3. **Lifetime-token perf:** caching strategy for Codex rollout summing — to be settled in `writing-plans`.

---

## 10. Validation

- Faithful preview via Rich `export_svg` → browser (used throughout design); reuse to verify implementation against the approved mockup.
- **Width regression guard (required):** a test that renders the widest real case (Codex + Spark, 30-char name) at `Console(width=85)` and asserts no physical line wraps past 85 and the longest account name appears intact on one row. This is the exact failure mode found during design — make it a permanent test. (Floor updated from 80 to 85; see §8 update above.)
- Recommend **snapshot tests** of rendered output (text + styles) for the grouped renderer, heat bands, window classification, and zero/active cells.

---

## Sign-off — RESOLVED (2026-06-19)

- **Spec approved.** Proceeding to `writing-plans`.
- **Plan-detection:** decision = **fold the `unknown → max` fix into this branch** (so the new TUI shows the correct `max` tag on launch). Tracked as §9.2; detection work kept in separate commits from the rendering work.
- **Masthead refinement approved 2026-07-09.** Use the robot masthead and
  provider-local account counts exactly as shown in §2. Do not add separate
  alert-count or `needs attention` wording to panel titles.

# Token Activity Accuracy Implementation Plan

> **For agentic workers:** Execute this plan one task at a time. Use
> `superpowers:subagent-driven-development` or
> `superpowers:executing-plans` when that capability is available. Preserve
> the stop/go gates, atomic migration boundary, and verification requirements
> when a different execution mechanism is used.

- **Status:** Implemented and verified
- **Date:** 2026-07-10
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Audited branch:** `develop`
- **Audited commit:**
  `18fe3b8811337101b8a6ca68cb7c26f26101c4a1`
- **Upstream:** `origin/develop`
- **Provider baseline:** Claude Code `2.1.207`; Codex CLI `0.144.1`
- **Codex source commit:**
  `44918ea10c0f99151c6710411b4322c2f5c96bea`
- **Implementation authority:** This plan, together with the provider sources
  linked in section 2.1

> **Implementation cohesion note (2026-07-10):** The architecture gate exposed
> `usage/service.py` and `usage/render.py` crossing the repository's 800-line
> review threshold during implementation. The approved responsibilities were
> preserved in two focused owners: `usage/activity.py` contains the
> scope-specific ports and aggregation policy, while
> `usage/activity_render.py` contains token precision, scope, and failure copy.
> The service still owns selection and eligibility; the overview still owns
> layout. This split changes no provider, model, or presentation contract.

**Verification snapshot (2026-07-10):** The implementation passes 795 tests
with four platform-specific skips, a full branch-coverage test run, Ruff,
`ty`, pre-commit, Bandit, dependency vulnerability scanning, Markdown lint,
architecture mutation tests, exact wheel verification, and an isolated wheel
smoke install. A real `uv run sidekick-usages` invocation on `develop`
confirmed Claude live-suffix collection and Codex account-profile collection.
The unchecked boxes below are retained as the original executable sequence;
this status and snapshot record its completed outcome.

## 1. Outcome

Replace Sidekick's incorrect, output-only local counters with truthful token
activity obtained from each provider's authoritative surface.

The completed implementation must provide all of the following:

- Claude totals equal Claude Code's local `/stats` and `/usage` total for the
  same installation and instant;
- Claude totals include non-cached input and output tokens, include live
  activity not yet folded into `stats-cache.json`, and exclude cache-read and
  cache-creation tokens;
- Claude activity is identified as local-installation history and is never
  attributed to one saved Sidekick account;
- Codex totals come from the account token-activity profile for each saved
  account, not local rollout logs or SQLite state;
- Codex activity is account-scoped and each eligible saved account is queried
  independently through Sidekick's existing pooled HTTP boundary, even when
  its separate rate-limit request has a non-authentication failure;
- multi-account Codex totals are summed only from successful account-scoped
  responses and are labeled `known tokens` whenever coverage is incomplete;
- no plausible-looking local Codex number is substituted for an unavailable
  account profile;
- wide provider panels show exact, grouped integer totals, while the narrow
  fallback keeps enough compact precision to expose useful change;
- activity failures remain explicit, typed, secret-safe outcomes and do not
  erase otherwise valid rate-limit rows;
- the obsolete cross-provider `lifetime.py` owner and Sidekick Codex rollout
  cache are removed from runtime, path, package, test, and documentation
  contracts; and
- the implementation adds no runtime dependency and does not mutate either
  provider's active CLI login or provider-owned activity state.

This is one correction, not a patch to the current collector. Provider-owned
adapters, a scope-aware domain model, application orchestration, rendering,
obsolete-code removal, tests, gates, and documentation form one complete
migration.

## 2. Source of truth and execution contract

### 2.1 Provider authority

The implementation is grounded in these provider-owned sources, retrieved and
rechecked on 2026-07-10:

- Anthropic documents that `CLAUDE_CONFIG_DIR` replaces `~/.claude`, that
  project and subagent transcripts live below `projects/`, and that
  `stats-cache.json` holds the aggregated token counts shown by `/usage`:
  [Claude application data][claude-data].
- Anthropic documents `/stats` as an alias for `/usage` and describes
  `/usage` as the activity-statistics surface:
  [Claude commands][claude-commands].
- The exact installed Codex release documents `account/usage/read` as the
  ChatGPT account token-activity summary and daily-bucket operation:
  [Codex app-server account API][codex-app-server].
- The exact installed Codex release builds the ChatGPT profile route as
  `/wham/profiles/me`:
  [Codex backend client][codex-client].
- The exact installed Codex TUI labels `summary.lifetime_tokens` as
  `Lifetime`:
  [Codex token chart][codex-chart].

The Claude merge details that are not documented as a public file schema were
verified against the installed `2.1.207` executable and reproduced against
the live provider-owned data. They are version-pinned implementation evidence,
not a claim that Anthropic has promised a stable transcript schema.

### 2.2 Provider drift gate

Before editing runtime code, rerun:

```bash
claude --version
codex --version
```

If Claude is no longer `2.1.207`, re-inspect the installed implementation for
all of these semantics before continuing:

- whether `modelUsage.inputTokens + modelUsage.outputTokens` remains the
  historical total;
- which date is excluded from the historical cache and rescanned live;
- the top-level and subagent transcript discovery patterns;
- parent-log `isSidechain` exclusion;
- the treatment of cache-read and cache-creation fields; and
- the handling of an incomplete final JSONL record during an active write.

If Codex is no longer `0.144.1`, identify the installed release commit and
recheck all of these items in upstream source:

- `account/usage/read` remains a stable app-server method;
- the ChatGPT profile route remains `/backend-api/wham/profiles/me`;
- `stats.lifetime_tokens` remains the TUI's lifetime value;
- `daily_usage_buckets` remains optional and bounded-history data; and
- the access-token and `ChatGPT-Account-Id` headers remain sufficient for a
  saved account request.

When provider behavior changed materially, stop the affected task, record the
new primary-source evidence in a tracked document, update this authority, and
obtain operator approval. Do not silently retain an old parser, scrape the
interactive TUI, or substitute a local approximation.

### 2.3 Repository and worktree discipline

Before every task:

- confirm the repository, branch, commit, and upstream relationship;
- inspect the complete worktree and preserve unrelated user changes;
- search the owning package for the exact concept name before adding a type,
  helper, map, constant, service, or dependency;
- read at least two neighboring implementation files and their tests;
- refresh every import, caller, path field, package member, and documentation
  reference affected by the task;
- write the smallest set of behavior-bearing tests that can disprove the
  implementation;
- run the focused gate before the complete gate; and
- inspect the diff for credentials, account identities, provider payloads,
  dead code, suppressions, and stale terminology.

The worktree at plan publication already contains an operator-requested
`AGENTS.md` refresh. Treat it as an existing user change. Preserve it and do
not stage it in a token-activity commit unless the operator explicitly asks
for that scope.

Do not commit directly to `main`. Use Conventional Commits. Do not commit or
push unless the operator requests those actions.

No tracked document may depend on ignored or local-only material. All evidence
needed to execute this plan is inlined here or linked to a stable primary
source.

### 2.4 Production-valid migration rule

The runtime migration is atomic. Local TDD steps may temporarily leave failing
imports or both implementations in the worktree, but no commit may contain:

- two active token-total owners;
- provider adapters that exist but are not composed;
- an `output_tokens` model presented as total activity;
- a Codex rollout, SQLite, or Sidekick-cache fallback;
- `AppContext.lifetime` after activity belongs to `UsageCheckResult`;
- a path contract for a cache that runtime no longer owns;
- a renderer that can receive activity from a different account selection
  than the rate-limit rows; or
- package and architecture gates that still require the deleted module.

The domain model, both provider adapters, usage-service integration, CLI and
render changes, obsolete-code deletion, test replacement, path changes, and
packaging gates therefore land in one production-valid runtime commit. The
internal task boundaries exist for disciplined TDD and review, not for
publishing half of the migration.

## 3. Audited baseline and reproduced defects

### 3.1 Live repository baseline

At the audited commit and with the existing `AGENTS.md` worktree change:

- Python is `3.14.6`;
- the package contains 33,746 Python source lines;
- tests contain 20,775 Python source lines and 438 source test functions;
- `uv run pytest -q` collects 798 cases and passes 794 with four expected
  platform skips;
- the 94 directly affected lifetime, usage-service, render, CLI-error, path,
  location, and architecture cases pass;
- `uv run ruff check src/ tests/` passes;
- `uv run ty check src/ tests/` passes; and
- the architecture check passes with eight pre-existing cohesion warnings.

Refresh this baseline if implementation starts from a different commit. A
clean baseline is not evidence that the current metric is correct: the 12
existing lifetime tests accurately pin the wrong output-only design.

### 3.2 Claude exact reproduction

Claude Code `2.1.207` produces its local `/stats` total by:

1. loading the historical `stats-cache.json`;
2. summing each model's `inputTokens + outputTokens`;
3. scanning the current cache boundary through the current UTC day from
   project and subagent JSONL transcripts;
4. merging those live values in memory;
5. excluding `cacheReadInputTokens` and `cacheCreationInputTokens`; and
6. formatting the resulting total with compact display precision.

The audited local values were:

```text
historical input        230,254,503
historical output       671,226,575
historical total        901,481,078

live UTC-day input          733,122
live UTC-day output       1,249,885
live UTC-day total        1,983,007

complete local total    903,464,085
Claude display               903.5m
```

The historical cache also records 1,041 sessions, which reconciles with the
CLI's rounded `1.0k` display.

Current `src/sidekick_usages/lifetime.py` reads only `outputTokens` from the
historical cache. It therefore reports `671,226,575`, exactly `232,237,510`
tokens short at the captured instant. It also never scans the live cache
boundary, so the value changes only when Claude refreshes its historical
cache and can appear frozen during active use.

The Claude number is installation history. The corpus is selected by
`CLAUDE_CONFIG_DIR` or `~/.claude`, not by a saved Sidekick label. Even when
most local history belongs to one organization, Sidekick cannot truthfully
assign the total to that label.

### 3.3 Codex exact reproduction

Codex `0.144.1` exposes two distinct metrics:

- `/status` shows current-session token usage; and
- `/usage`, backed by `account/usage/read`, returns the ChatGPT account's
  token-activity profile.

The audited account profile returned:

```text
lifetime tokens              7,449,473,297
peak daily tokens              749,395,781
longest running turn                23,463 seconds
current streak                           1 day
longest streak                          29 days
daily buckets                           65
sum of returned buckets      7,449,473,297
```

The same profile value was reproduced through the installed Codex app server
and through Sidekick's existing pooled HTTP client against:

```text
https://chatgpt.com/backend-api/wham/profiles/me
```

Current `src/sidekick_usages/lifetime.py` instead walks local rollout JSONL
files and takes each file's maximum cumulative `output_tokens`. It reports
`265,238,378`, exactly explaining the current `265M output` subtitle, but that
number is not the account-lifetime feature.

The audited local populations were:

```text
raw local cumulative total        68,865,568,546
local cached input                66,803,814,528
local output                         265,238,378
local non-cached input + output    2,061,495,618
account lifetime                  7,449,473,297
```

The local SQLite sum increased from `68,555,849,247` to `68,556,480,760`
during diagnosis, a `631,513` increase. Local state is updating; it is simply
a different population with different cache accounting, retention, account
scope, and remote-history coverage. No local formula can be used as an honest
fallback for the account profile.

### 3.4 Display precision defect

The current renderer formats millions with no decimal places and billions
with one decimal place. It can hide changes below roughly half a million and
50 million tokens respectively, even after the data source is corrected.

The required display contract is:

```text
wide Claude     903,464,085 tokens
narrow Claude   903.46M tokens

wide Codex      7,449,473,297 tokens
narrow Codex    7.449B tokens
```

The term `output` must disappear because both corrected providers expose total
non-cached token activity, not output-only counts.

## 4. Approved architecture decisions

### TA-01: Model token activity and scope, not lifetime output

Add `TokenActivityScope` to `core/types.py` with exactly these values:

```python
class TokenActivityScope(StrEnum):
    ACCOUNT = "account"
    LOCAL_INSTALLATION = "local_installation"
```

Add an immutable, slotted `TokenActivitySummary` to `core/models.py`:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class TokenActivitySummary:
    total_tokens: int
    scope: TokenActivityScope
    since: date | None
```

The model rejects Booleans, non-integers, and negative totals. It stores no
provider payload, account credential, display string, or cache accounting.
`since` is optional because the Codex lifetime endpoint does not expose a
trustworthy all-time start date.

Add one explicit `TokenActivityUnavailable` core outcome and a PEP 695 union
for provider-source reads. Do not use `None`, zero, or an exception message to
represent absence.

### TA-02: Keep provider reads distinct from application aggregation

`providers/claude/activity.py` owns Claude path resolution, bounded filesystem
discovery, historical-cache parsing, transcript parsing, and local
aggregation. It returns a local-installation-scoped core reading or raises the
existing typed provider-boundary failure.

`providers/codex/activity.py` owns the profile route, authenticated GET, and
conversion of a validated response to an account-scoped core summary. It
returns one summary for one account or raises the existing typed HTTP or
provider-boundary failure.

`usage/service.py` owns selection, call timing, per-account collection,
cross-account aggregation, partial coverage, and failure mapping. Provider
modules must not import `usage.models` or construct Rich values.

Use two narrow structural ports because local-installation and account reads
have irreducibly different call shapes:

```python
class LocalTokenActivitySource(Protocol):
    provider_id: ProviderId

    def read(
        self,
        reference_time: datetime,
    ) -> TokenActivityReading: ...


class AccountTokenActivitySource(Protocol):
    provider_id: ProviderId

    def read(
        self,
        account: Account,
        http: HttpClient,
    ) -> TokenActivityReading: ...
```

These ports are required I/O seams, not speculative framework abstractions.
Inject provider-id mappings into `UsageCheckService`; do not add activity
methods to the base `Provider` class because the two scopes do not share one
truthful signature.

### TA-03: Make aggregate completeness unrepresentable as a bare number

Add closed application outcomes in `usage/models.py`:

- `CompleteTokenActivity` contains a provider id and one scope-aware summary;
- `PartialTokenActivity` contains an account-scoped known summary, positive
  covered-account count, larger selected-account count, and any profile-read
  issues;
- `UnavailableTokenActivity` contains provider id and scope but no numeric
  total;
- `FailedTokenActivity` contains provider id, scope, and one or more typed,
  secret-safe issues; and
- `ProviderTokenActivity` is the PEP 695 union of those variants.

Add `TokenActivityIssue` with a closed `TokenActivityFailureKind` vocabulary:

- `SOURCE_UNREADABLE`;
- `SOURCE_MALFORMED`;
- `AUTHENTICATION`;
- `FORBIDDEN`;
- `RATE_LIMITED`;
- `TRANSIENT`; and
- `PROVIDER`.

An issue may carry an account label only for account-scoped collection. It
must never contain a token, response body, transcript content, project name,
or credential path. Validate all variant invariants in `__post_init__` so a
partial result cannot claim full coverage and a failed result cannot be empty.

Extend `UsageCheckResult` with provider activity outcomes from the same
selection and `reference_time` as its rows. The CLI must not perform a second,
independent collection after receiving the service result.

### TA-04: Match Claude `2.1.207` without writing Claude state

Claude activity collection must:

1. resolve `CLAUDE_CONFIG_DIR`, falling back to `~/.claude`;
2. receive the resolved directory through the adapter constructor so tests do
   not monkeypatch module globals;
3. read `stats-cache.json` with an explicit byte bound and strict UTF-8 JSON
   decoding;
4. validate `modelUsage`, `lastComputedDate`, and optional
   `firstSessionDate` with private strict Pydantic models;
5. sum every model's `inputTokens + outputTokens`;
6. validate but exclude `cacheReadInputTokens` and
   `cacheCreationInputTokens` when present;
7. treat the cache as history before its inclusive live-scan boundary and
   scan assistant events dated from `lastComputedDate` through the current
   UTC date;
8. when the cache is absent, scan the available transcript corpus and derive
   `since` from the earliest valid event;
9. reject a future cache boundary rather than silently returning stale or
   double-counted data;
10. discover top-level session logs and actual subagent logs under the
    provider-documented `projects/` layout;
11. exclude assistant records marked `isSidechain` in a parent session log;
12. include assistant records from actual subagent files, regardless of the
    parent-log sidechain marker convention;
13. parse event timestamps as aware instants and classify dates in UTC;
14. sum only `message.usage.input_tokens + output_tokens`;
15. validate but exclude transcript cache-read and cache-creation counts;
16. stream files and lines under explicit bounds instead of loading a
    transcript into memory;
17. capture a file-size snapshot and ignore only an unterminated final
    fragment at that snapshot boundary, which may be an in-progress provider
    write;
18. fail the entire local total on a malformed complete assistant record or
    an unreadable selected source rather than presenting a partial number;
19. use modification time only to skip a file that is provably older than the
    live-scan boundary, never as the event date or token authority;
20. reject symlinks and non-regular selected source files;
21. never write, refresh, rename, lock, or delete Claude-owned data; and
22. return `LOCAL_INSTALLATION` scope with no saved account label.

Use these provider-local safety bounds:

```text
stats-cache.json                 16 MiB
one transcript JSONL record     32 MiB
selected activity files        100,000
one token count and final sum   signed 64-bit maximum
```

The audited cache and 1,041-session history are far below those limits, while
the 32 MiB record bound leaves headroom for assistant records before Claude's
documented tool-result spilling takes effect. Exceeding a bound is a safe
malformed or unreadable outcome, never a partial total. Keep the constants in
the Claude provider module with their rationale. Do not reuse persistence
document limits merely because both values are byte counts.

### TA-05: Use the Codex account profile directly

Codex activity collection must:

1. call
   `https://chatgpt.com/backend-api/wham/profiles/me` with `GET`;
2. reuse `HttpClient.get_json`, including HTTPS enforcement, response bounds,
   pooling, retry ownership, timeouts, and typed status translation;
3. reuse the saved access token and resolved `ChatGPT-Account-Id` for the
   specific `Account` passed to the adapter;
4. use the exact provider header name `ChatGPT-Account-Id` in tests and code;
5. validate `stats.lifetime_tokens` as a required, non-Boolean,
   non-negative signed 64-bit integer;
6. validate optional non-negative summary metrics without adding them to the
   public domain model until a product surface uses them;
7. validate every returned `daily_usage_buckets` item, its ISO date, and its
   non-negative token count when the optional list is present;
8. reject duplicate bucket dates and malformed, Boolean, negative, or
   overflowing values;
9. treat `lifetime_tokens` as authoritative and never recompute it from the
   optional buckets because the provider may return a bounded activity
   window;
10. set `since=None` because the returned bucket range is not proof of the
    account's all-time start date;
11. perform one profile request for each eligible saved Codex account,
    independently of whether its separate rate-limit endpoint succeeded;
12. surface HTTP and schema failures through typed activity issues;
13. never read `~/.codex/sessions`, Codex SQLite, or a Sidekick rollout cache;
    and
14. never invoke Codex with a private credential home or stage a temporary
    auth file.

After activity becomes the third Codex ChatGPT request caller, extract the
already-repeated account-id resolution and base authenticated headers into one
provider-local request helper. Use it from usage, heartbeat, and activity.
Keep endpoint-specific `Accept`, beta, and operation headers at each owning
call site. Replace the stale version-specific user-agent literal with the
exact release's stable `codex-cli` fallback unless refreshed primary-source
evidence requires a different value.

### TA-06: Aggregate Codex accounts honestly

For each selected Codex account:

- separate credential preparation from the two independent read outcomes;
- retain the latest trustworthy in-memory `Account` after the existing expiry,
  refresh, one-auth-retry, account-id discovery, and persistence rules;
- consider the account activity-eligible when credentials and identity remain
  structurally valid and durable, even if the rate-limit endpoint returned a
  forbidden, rate-limited, transient, or provider-payload failure;
- consider the account ineligible after unknown-provider, invalid-expiry,
  unrecovered authentication, rejected refresh, or persistence failure;
- issue the profile read for every eligible account so a rate-limit endpoint
  failure does not silently remove otherwise authoritative activity;
- do not start a second credential-refresh loop if the profile request returns
  `401`;
- map a profile `401` to an authentication activity issue with explicit
  refresh guidance; do not mutate credentials outside the canonical explicit
  or normal refresh workflow; and
- keep store order for deterministic requests and results.

Then reduce outcomes as follows:

| Covered profile reads | Selected accounts | Result |
|---:|---:|---|
| all | one or more | `CompleteTokenActivity` with their exact sum |
| some | more than covered | `PartialTokenActivity` and `known tokens` |
| none | all accounts were activity-ineligible | unavailable account activity |
| none | one or more profile reads failed | `FailedTokenActivity` |

Normal account failures already contribute `MANUAL_ACTION`. A partial result
caused only by those failed rows does not manufacture an extra system error.
A malformed, unreadable, transport, authentication, or status failure during
an attempted activity read contributes `SYSTEM_ERROR`; the CLI uses the
highest existing exit code.

Claude is collected once whenever at least one Claude account was selected,
even if all Claude rate-limit requests failed, because its scope is independent
of saved account credentials.

### TA-07: Render exact wide totals and precise compact totals

`usage/render.py` receives only completed `UsageCheckResult` data and performs
no filesystem, HTTP, credential, or aggregation work.

In the framed panel mode, render exact grouped integers:

```text
903,464,085 tokens  ·  local CLI  ·  since Dec 28
7,449,473,297 tokens
7,449,473,297 known tokens
```

In the narrow legacy fallback, render compact values:

```text
903.46M tokens  ·  local
7.449B tokens
7.449B known tokens
```

Use two explicit formatters rather than a Boolean mode flag:

- exact formatting uses Python's grouped integer format;
- compact thousands and millions retain up to two decimal places;
- compact billions retain up to three decimal places; and
- trailing zeroes and a trailing decimal point are removed.

The 85-column framed-panel floor remains unchanged. Exact activity subtitles
must participate in panel measurement and must not wrap or force an otherwise
valid 85-column fixture into the legacy view. The compact formatter is for the
intentional narrow fallback, not a workaround for panel measurement.

Render explicit unavailable and failure labels without cache terminology.
Never render `output`, `lifetime cache`, a false zero, or a complete Codex
total when coverage is partial.

When a partial or failed account-scoped outcome contains activity-read issues,
render a concise provider-panel warning for each affected saved label below
the normal usage rows. Reuse the existing safe refresh-command construction
for authentication issues. Keep the valid usage row visible; do not replace it
with an activity warning or expose a raw provider message.

### TA-08: Remove the obsolete cache contract without hidden deletion

Delete the Codex rollout parser, incremental cache, cache failure kinds, and
all current tests that exist only to pin them. Remove
`ApplicationPaths.lifetime_cache_file` from production and test constructors,
architecture checks, path tests, location tests, support fixtures, and docs.

An existing `codex-lifetime-cache.json` becomes inert after upgrade. Do not
delete it during a read-only usage invocation: hidden cleanup would add a
filesystem mutation, create a new failure path, and reduce rollback safety.
Document that an older derived cache can be removed manually. It must never be
read, migrated, trusted, or rewritten by the corrected runtime.

### TA-09: Correct active authority while preserving history

The June TUI specification currently says output-only local counts are the
only comparable cross-provider value. That decision is disproved. Update
active specifications to the scope-aware design and add dated supersession
notes to executed plans, research, and completion records that mention the old
collector or cache.

Do not rewrite historical execution steps as though they never happened.
Clearly distinguish:

- the behavior that was implemented and verified at that time; and
- the later provider-source evidence that supersedes the metric and owner.

Update README and persistence documentation only in the same publication
sequence as the runtime behavior they describe.

### TA-10: Add no dependency

The build-versus-adopt decision is closed for this feature:

- Codex app-server is official, but using it for multiple saved OAuth accounts
  would require pointing it at Sidekick private credential homes or staging
  temporary secret auth files. Isolated access-token injection was tested and
  rejected as unauthenticated for this endpoint.
- The direct account-profile request succeeds through the existing pooled
  `HttpClient`, returns the app-server value exactly, and preserves account
  isolation and active-login safety.
- Claude exposes no equivalent machine-readable statistics command. Parsing
  its provider-owned cache and live JSONL suffix is the narrow necessary local
  adapter.
- Parsing either interactive TUI, adding a Node usage tool, adding Tenacity,
  or adding a second HTTP client would increase brittleness and maintenance
  without improving the authority of the result.
- Pydantic and the shared HTTP/retry stack are already approved dependencies
  and cover the required validation and transport behavior.

Reopen this decision only if a provider ships a documented non-interactive
multi-account activity interface that works with isolated saved credentials
without modifying provider state.

[claude-data]: https://code.claude.com/docs/en/claude-directory
[claude-commands]: https://code.claude.com/docs/en/commands
[codex-app-server]: https://github.com/openai/codex/blob/44918ea10c0f99151c6710411b4322c2f5c96bea/codex-rs/app-server/README.md#L1926-L1937
[codex-client]: https://github.com/openai/codex/blob/44918ea10c0f99151c6710411b4322c2f5c96bea/codex-rs/backend-client/src/client.rs#L314-L325
[codex-chart]: https://github.com/openai/codex/blob/44918ea10c0f99151c6710411b4322c2f5c96bea/codex-rs/tui/src/chatwidget/tokens/chart.rs#L155-L169

## 5. Target ownership and file map

### 5.1 Final package shape

Only the relevant target subtree is shown:

```text
src/sidekick_usages/
├── core/
│   ├── __init__.py
│   ├── models.py
│   └── types.py
├── providers/
│   ├── claude/
│   │   ├── __init__.py
│   │   ├── activity.py
│   │   └── schemas.py
│   └── codex/
│       ├── __init__.py
│       ├── activity.py
│       ├── heartbeat.py
│       ├── request.py
│       ├── schemas.py
│       └── usage.py
├── usage/
│   ├── __init__.py
│   ├── activity.py
│   ├── activity_render.py
│   ├── models.py
│   ├── render.py
│   └── service.py
├── cli/
│   ├── context.py
│   └── commands/
│       └── usage.py
└── paths.py
```

`src/sidekick_usages/lifetime.py` does not exist in the final tree.

### 5.2 Ownership map

| Current owner | Final owner | Reason |
|---|---|---|
| `lifetime.py:LifetimeTotal` | `core/models.py:TokenActivitySummary` | Provider-neutral truth and explicit scope |
| `lifetime.py:LifetimeUnavailable` | Core reading plus usage outcome | Absence cannot become zero |
| `lifetime.py:LifetimeFailureKind` | `usage/models.py` activity issues | Application policy and presentation need typed causes |
| `lifetime.py:_claude_total` | `providers/claude/schemas.py` | Claude payload validation belongs to Claude |
| `lifetime.py:claude_lifetime_output` | `providers/claude/activity.py` | Claude filesystem and aggregation boundary |
| Codex rollout functions | Deleted | Wrong metric and wrong scope |
| Codex cache functions | Deleted | No corrected feature consumes this cache |
| `LifetimeCollector` | `usage/activity.py:TokenActivityCollector`, called by `UsageCheckService` | One selection and one completed result |
| CLI lifetime collection | `UsageCheckService.check` | CLI renders; it does not collect data |
| `_lifetime_text` | Token-activity text in `usage/activity_render.py` | Scope, coverage, and precision-aware copy |
| Codex account headers | `providers/codex/request.py` | Exact third-call-site reuse |
| `ApplicationPaths.lifetime_cache_file` | Deleted | No Sidekick runtime cache remains |

### 5.3 File operations

Create:

- `src/sidekick_usages/providers/claude/activity.py`;
- `src/sidekick_usages/providers/codex/activity.py`;
- `src/sidekick_usages/providers/codex/request.py`;
- `src/sidekick_usages/usage/activity.py`;
- `src/sidekick_usages/usage/activity_render.py`;
- `tests/test_claude_activity.py`; and
- `tests/test_codex_activity.py`;
- `tests/test_usage_activity.py`.

Modify:

- `src/sidekick_usages/core/models.py`;
- `src/sidekick_usages/core/types.py`;
- `src/sidekick_usages/core/__init__.py` only if its current public facade
  exports neighboring core values;
- `src/sidekick_usages/providers/claude/schemas.py`;
- `src/sidekick_usages/providers/claude/__init__.py`;
- `src/sidekick_usages/providers/codex/schemas.py`;
- `src/sidekick_usages/providers/codex/usage.py`;
- `src/sidekick_usages/providers/codex/heartbeat.py`;
- `src/sidekick_usages/providers/codex/__init__.py`;
- `src/sidekick_usages/usage/models.py`;
- `src/sidekick_usages/usage/service.py`;
- `src/sidekick_usages/usage/render.py`;
- `src/sidekick_usages/usage/__init__.py`;
- `src/sidekick_usages/cli/context.py`;
- `src/sidekick_usages/cli/commands/usage.py`;
- `src/sidekick_usages/paths.py`;
- `tests/test_core_models.py`;
- `tests/test_core_types.py`;
- `tests/test_usage_service.py`;
- `tests/test_render.py`;
- `tests/test_check_errors.py`;
- `tests/test_paths.py`;
- `tests/test_persistence_location_service.py`;
- `tests/test_support.py`;
- `packaging/check_architecture.py`;
- `packaging/smoke_wheel.py`;
- `README.md`;
- `docs/persistence-and-recovery.md`;
- the active Superpowers specifications; and
- historical Superpowers plans, research, and completion records only where a
  dated supersession note is required.

Delete:

- `src/sidekick_usages/lifetime.py`; and
- `tests/test_lifetime.py`.

Refresh exact references immediately before editing. Do not modify a listed
file when the live search proves that it no longer owns the concept.

### 5.4 Dependency direction

The final dependency direction is:

```text
core models and types
        ↑
provider activity adapters ──→ shared HTTP and provider schemas
        ↑
usage service and result aggregation
        ↑
usage renderer and CLI command adapter
```

Enforce these constraints:

- `core/` imports no provider, HTTP, filesystem, path, usage, or CLI code;
- Claude activity imports core, serialization, and Claude schema boundaries,
  but not Codex or usage presentation;
- Codex activity imports core, shared HTTP, and Codex boundaries, but not
  Claude, filesystem traversal, or usage presentation;
- provider modules never import `usage.models`;
- `usage/activity.py` owns the structural ports and aggregates core readings;
- `usage/service.py` owns selection, credential preparation, and eligibility;
- `usage/activity_render.py` owns token-activity text and precision;
- `usage/render.py` receives immutable application results and owns layout;
- `cli/commands/usage.py` selects, renders, and exits but does not call a
  provider or filesystem directly; and
- `paths.py` remains the sole owner of Sidekick application paths, while the
  Claude adapter owns the provider's `CLAUDE_CONFIG_DIR` discovery.

### 5.5 Module-size budget

At the audited baseline:

- `core/models.py` is 242 lines;
- `core/types.py` is 97 lines;
- Claude schemas are 573 lines;
- Codex schemas are 642 lines;
- `usage/models.py` is 153 lines;
- `usage/service.py` is 412 lines;
- `usage/render.py` is 679 lines; and
- `cli/context.py` is 704 lines.

Keep every changed module below the 1,000-line hard cap. Review cohesion at
approximately 800 lines. If schemas or composition approach that target,
extract only a cohesive provider-local module with concrete call sites; do not
create a generic dumping ground.

## 6. Detailed data and behavior contracts

### 6.1 Core reading contract

The core union distinguishes a valid zero from absence:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class TokenActivityUnavailable:
    scope: TokenActivityScope


type TokenActivityReading = (
    TokenActivitySummary | TokenActivityUnavailable
)
```

Both variants are infrastructure-free. Provider adapters raise existing typed
errors for malformed or unreadable sources; they do not place error strings in
core.

### 6.2 Application aggregate contract

The application layer wraps core readings with provider and coverage context.
The precise field names may follow neighboring dataclass conventions, but the
following states and invariants are mandatory:

```text
CompleteTokenActivity
  provider_id
  summary

PartialTokenActivity
  provider_id
  summary                 # known account-scoped sum
  covered_accounts        # > 0
  selected_accounts       # > covered_accounts
  issues                  # may be empty if skipped rows explain the gap

UnavailableTokenActivity
  provider_id
  scope

FailedTokenActivity
  provider_id
  scope
  issues                  # non-empty
```

The result for a provider appears at most once in `UsageCheckResult`. Provider
order follows the selected account store order. A valid zero remains complete
when every selected source was read successfully.

### 6.3 Claude cache boundary

Private Pydantic models in `providers/claude/schemas.py` validate only fields
that the feature consumes or whose validity affects accounting:

```text
stats cache
  modelUsage: mapping[str, model usage]
  lastComputedDate: ISO date
  firstSessionDate: optional aware timestamp

model usage
  inputTokens: non-negative integer
  outputTokens: non-negative integer
  cacheReadInputTokens: optional non-negative integer
  cacheCreationInputTokens: optional non-negative integer
```

Unknown provider fields remain allowed so additive schema evolution does not
break Sidekick. Required consumed fields remain strict. Booleans are never
accepted as integers.

The historical subtotal is:

```text
sum(model.inputTokens + model.outputTokens for every model)
```

It explicitly does not include either cache token field.

### 6.4 Claude transcript boundary

Parse the JSON discriminator before validating assistant usage. Non-assistant
records are ignored after strict JSON decoding. Relevant assistant records
must provide:

```text
type: "assistant"
timestamp: aware provider timestamp
isSidechain: optional Boolean
message.usage.input_tokens: non-negative integer
message.usage.output_tokens: non-negative integer
message.usage.cache_read_input_tokens: optional non-negative integer
message.usage.cache_creation_input_tokens: optional non-negative integer
```

Use file provenance to apply sidechain ownership:

- parent session file plus `isSidechain=true`: exclude;
- parent session file without that marker: include; and
- actual subagent file: include.

Do not use a global seen-message heuristic, message text, request id, or model
name to deduplicate. Those would introduce undocumented identity assumptions.

### 6.5 Claude date and concurrency contract

Acquire one aware `reference_time` from the injected application clock and
convert it to UTC. Never call `datetime.now` in the adapter.

With a cache:

- validate `lastComputedDate <= reference_time.date()`;
- use the historical model subtotal as the provider's pre-boundary aggregate;
- scan complete assistant records whose UTC date is greater than or equal to
  `lastComputedDate` and no later than the reference UTC date; and
- use the cache's `firstSessionDate` for `since` when valid.

Without a cache:

- return unavailable only when no valid transcript corpus exists;
- otherwise scan the available corpus through the reference date; and
- derive `since` from the earliest included event.

When a cache has a positive historical total but omits `firstSessionDate`, do
not claim that the earliest live event is the history start. Return `since=None`.

For an actively written transcript:

- open without following a symlink;
- capture identity and size before parsing;
- parse only complete newline-terminated records within that snapshot;
- ignore one terminal unterminated fragment at snapshot EOF;
- reject any malformed complete line; and
- let records appended after the snapshot appear on the next invocation.

This yields a consistent, bounded read without locking or modifying Claude
Code's file.

### 6.6 Codex profile boundary

Private strict Pydantic models in `providers/codex/schemas.py` validate this
provider shape:

```text
profile
  stats
    lifetime_tokens: required non-negative integer
    peak_daily_tokens: optional non-negative integer
    longest_running_turn_sec: optional non-negative integer
    current_streak_days: optional non-negative integer
    longest_streak_days: optional non-negative integer
    daily_usage_buckets: optional list

daily bucket
  start_date: ISO date
  tokens: non-negative integer
```

Allow additive unknown fields. Reject a missing or null `lifetime_tokens` as
an incomplete provider boundary because the feature cannot truthfully present
an account total without it.

Do not assert that bucket sum equals lifetime. The observed response happened
to reconcile, but upstream types make the bucket list optional and its visible
range is not an all-time contract.

### 6.7 Account freshness and mutation contract

The current usage service may refresh credentials, derive and persist a Codex
account id, or update a provider-discovered plan while attempting a row.
Activity collection must use the latest trustworthy post-preparation
`Account`, not the stale object selected before refresh and not an account
whose required state failed to persist.

Retain that eligibility and account in a closed private checked-account result
while building the public `AccountUsage` or `FetchFailure`. A forbidden,
rate-limited, transient, or malformed rate-limit response does not by itself
invalidate otherwise durable credentials and therefore does not suppress the
independent profile read. Do not expose credentials through
`UsageCheckResult`, repr, logging, or rendering.

The profile GET is a safe read and uses the existing `SAFE_READ` retry policy.
It does not mutate the account or provider. If it fails authentication after
credential preparation, record the failure and stop for that account; do not
refresh twice in one invocation.

### 6.8 Exit contract

The usage command reduces exit state once after rendering:

- no account or only manual account failures: existing manual-action policy;
- complete or unavailable activity without operational issues: no additional
  error;
- partial activity with no activity-read issue: no additional error beyond
  the account failure that caused incomplete coverage;
- partial activity with one or more activity-read issues: `SYSTEM_ERROR`;
- failed activity: `SYSTEM_ERROR`; and
- the highest existing `ExitCode` wins.

The TUI always renders valid usage rows and truthful activity state before a
non-zero exit. An activity failure must never discard a valid rate-limit row.

### 6.9 Presentation contract

Panel title account counts remain unchanged and include successful and failed
selected accounts. The token subtitle represents the provider activity
outcome, not the number of visible successful rows.

Required wording:

| State | Wide panel | Narrow fallback |
|---|---|---|
| Claude complete | exact `tokens`, `local CLI`, optional `since` | compact `tokens`, `local` |
| Codex complete | exact `tokens` | compact `tokens` |
| Codex partial | exact `known tokens` | compact `known tokens` |
| unavailable | `token activity unavailable` | same |
| unreadable | `token activity source unreadable` | same |
| malformed | `token activity source malformed` | same |
| authentication issue | `token activity authentication failed` | same |
| forbidden issue | `token activity forbidden` | same |
| rate-limited issue | `token activity rate limited` | same |
| transient/profile issue | `token activity temporarily unavailable` | same |

Per-account usage-failure rows retain their existing actionable refresh
guidance. Account-scoped activity issues use a separate concise warning row so
an otherwise valid usage row remains intact and the cause is explicit. Do not
repeat an account label or sensitive provider response in a panel subtitle.

## 7. Meaningful testing strategy

### 7.1 Test standard

Tests exist to pin the reported defect, scope truth, failure policy, and user
contract. They do not mirror every helper or every Pydantic field.

Delete the current rollout-cache tests with the rollout implementation. Do not
rename them and preserve their assertions. Prefer roughly 10 to 14 new or
materially changed cases across the owning suites; exceed that range only for
a distinct acceptance risk.

Every fixture uses synthetic project names, account labels, tokens, account
ids, and responses. Automated tests never read real provider homes, real
Sidekick state, or the network.

### 7.2 Claude live parity test

One load-bearing test in `tests/test_claude_activity.py` must create:

- a cache with historical input `230,254,503`;
- historical output `671,226,575`;
- very large cache-read and cache-creation values;
- an inclusive current UTC boundary;
- one current-day parent assistant input of `733,122`;
- current-day output of `1,249,885`; and
- synthetic non-assistant noise.

The first read must equal exactly:

```text
230,254,503 + 671,226,575 + 733,122 + 1,249,885
= 903,464,085
```

Then append one complete current-day assistant event, read again, and assert
that the total increases by exactly its input plus output without rewriting
the historical cache. Also assert the cache bytes are unchanged. This one test
catches the omitted input field, live-day freshness, cache-token exclusion,
and provider-state write regression.

### 7.3 Claude transcript ownership test

One test must include:

- a normal parent assistant event;
- a parent `isSidechain=true` assistant copy with an obviously large count;
  and
- the real assistant event in a subagent file.

Assert that the normal parent and actual subagent values are included once and
the parent sidechain copy is excluded. Do not unit-test the private discovery
helpers separately.

### 7.4 Claude failure-boundary test

One concise parameterized test may cover only behaviorally distinct source
states:

- no cache and no transcripts returns explicit unavailable;
- a malformed complete assistant usage record raises a safe malformed
  provider failure;
- an unreadable selected source raises a safe unreadable provider failure;
  and
- an unterminated snapshot-final fragment is ignored until complete.

Do not add a separate case for every invalid numeric field when the same strict
adapter and failure state already covers them.

### 7.5 Codex account-profile test

One test in `tests/test_codex_activity.py` must return a strict synthetic
profile with `lifetime_tokens=7_449_473_297` and valid daily buckets. Assert:

- the exact HTTPS route;
- bearer authorization is present without exposing it in assertion output;
- the exact saved account identity header is present;
- the result is account-scoped;
- `since is None`;
- the exact lifetime value is retained; and
- no filesystem collaborator exists or is called.

The fake HTTP client must capture the request and never use the network.

### 7.6 Codex schema test

Use one parameterized boundary test for `lifetime_tokens` values that are:

- missing;
- Boolean;
- negative; and
- above signed 64-bit range.

Each must produce the existing safe incomplete or malformed provider boundary,
not zero, coercion, or a raw Pydantic error. Add one malformed bucket case in
the same suite only if it exercises a different safe-field path.

### 7.7 No false Codex fallback test

When the profile fake raises authentication or transient failure, assert that
the service returns an explicit activity issue and never a numeric summary.
Do not create rollout files for this test: the strongest contract is that the
new adapter has no filesystem input and the architecture gate forbids the old
source terms.

### 7.8 Service aggregation tests

Keep two service-level tests:

1. A Claude provider with multiple saved accounts is read once and produces
   one local-installation result, while two successful Codex account profiles
   produce one exact complete sum in store order. One account's rate-limit
   endpoint may fail transiently while its independent profile still
   contributes to complete activity coverage.
2. One successful Codex profile plus one activity-ineligible selected account
   produces a partial known sum. A profile failure for an eligible account
   produces an activity issue, keeps any valid usage row, and contributes the
   system exit policy.

Reuse the existing fake provider, HTTP, clock, store, and credential seams.
Do not duplicate credential-refresh coverage already owned by
`tests/test_usage_service.py`.

### 7.9 Rendering tests

Use one parameterized wide/narrow rendering contract to assert:

- `903,464,085 tokens` and `903.46M tokens`;
- `7,449,473,297 tokens` and `7.449B tokens`;
- the Claude local-scope wording;
- `known tokens` for partial Codex coverage;
- an explicit, secret-safe authentication warning and refresh guidance for a
  profile issue without removing the account's valid usage row;
- absence of the word `output`; and
- no wrapped physical line in the existing 85-column worst-case fixture.

Assert only intentional product copy and width behavior. Do not snapshot the
entire Rich rendering or copy every account row into a new test.

### 7.10 CLI and architecture tests

Update the existing CLI error test to prove that activity operational failure
renders before a system-error exit and that partial coverage caused only by a
normal account failure retains the existing manual-action exit.

Update the architecture and wheel gates to prove:

- both new provider activity modules ship;
- `sidekick_usages/lifetime.py` does not ship;
- `AppContext` has no `lifetime` field;
- `ApplicationPaths` has no lifetime cache field;
- production imports no `sidekick_usages.lifetime`;
- provider modules do not import usage presentation;
- rollout parsing and cache names cannot return to production; and
- core remains infrastructure-independent.

These mechanical assertions replace implementation-coupled cache tests with a
durable ownership boundary.

## 8. Global task protocol

Apply this protocol to every change set below:

1. Run the repository and provider drift checks.
2. Read the complete target files and at least two neighboring owners.
3. Search the owning package for the exact concept names.
4. List the current callers, tests, docs, and gate assertions.
5. Write or revise a test that fails for the intended behavioral reason.
6. Run the smallest test command and confirm the expected failure.
7. Implement the complete owner without speculative hooks or fallbacks.
8. Run Ruff format, focused Ruff, focused `ty`, and the focused tests.
9. Inspect the diff and rerun the exact search for stale owners.
10. Do not commit until the change set's production-valid boundary is green.

If a test passes before implementation, stop and strengthen or delete it. Do
not keep an inert test as evidence.

If an external schema observation contradicts section 4, stop and update the
tracked authority before changing the product contract.

## 9. Change sets

### CS-TA-00: Refresh baseline and freeze source evidence

**Purpose:** Prove the plan still matches the live repository and installed
provider versions before any implementation edit.

**Files:**

- Read: `AGENTS.md`
- Read: `pyproject.toml`
- Read: all files in section 5.3
- Modify this plan only if a baseline fact drifted

**Steps:**

- [ ] Confirm the checkout and preserve worktree changes:

  ```bash
  pwd
  git branch --show-current
  git rev-parse HEAD
  git status --short --branch
  git diff -- AGENTS.md
  ```

  Expected repository is `/home/sabossedgh/dev/sidekick-usages`, branch is
  `develop`, and the existing `AGENTS.md` change remains intact.

- [ ] Confirm provider versions:

  ```bash
  claude --version
  codex --version
  ```

  Apply the section 2.2 drift gate if either version changed.

- [ ] Re-run exact ownership searches:

  ```bash
  rg -n "Lifetime|lifetime|output tokens|output-only|stats-cache|lifetime_cache|_format_tokens" src tests packaging README.md docs
  rg -n "ChatGPT-Account-Id|User-Agent|codex-cli" src/sidekick_usages/providers/codex tests
  rg -n "CLAUDE_CONFIG_DIR|isSidechain|inputTokens|outputTokens" src tests docs
  ```

- [ ] Record current module line counts and verify no target owner moved above
  the size limit.

- [ ] Run the baseline gates:

  ```bash
  uv run pytest -q
  uv run ruff check src/ tests/
  uv run ty check src/ tests/
  uv run python packaging/check_architecture.py
  git diff --check
  ```

**Stop/go gate:** Do not continue on a red baseline until the failure is
classified as a pre-existing operator change or corrected separately. Never
weaken a gate to proceed.

### CS-TA-01: Correct tracked design authority

**Purpose:** Remove the disproved output-only decision from active design
authority and make historical supersession explicit before code changes.

**Files:**

- Modify:
  `docs/superpowers/specs/2026-06-19-usage-tui-redesign-design.md`
- Modify:
  `docs/superpowers/specs/2026-07-09-maintainable-application-architecture-design.md`
- Modify:
  `docs/superpowers/plans/2026-06-19-usage-tui-redesign.md`
- Modify:
  `docs/superpowers/plans/2026-07-09-maintainable-application-architecture.md`
- Modify:
  `docs/superpowers/research/2026-07-10-application-path-discovery-dependency.md`
- Modify:
  `docs/superpowers/completion/2026-07-10-maintainable-application-architecture.md`
- Include this plan in the same documentation review boundary

**Steps:**

- [ ] Add a dated correction to the June TUI design and replace its active
  lifetime-output section with:

  - Claude local-installation total activity;
  - Codex account-lifetime profile activity;
  - explicit scope and partial coverage;
  - exact and compact formatting; and
  - no Codex rollout cache.

- [ ] Update the maintainable architecture design's target tree, core model,
  usage result, composition, path, failure, testing, and acceptance sections.
  Remove normative requirements for `lifetime.py`, `LifetimeCollector`,
  `AppContext.lifetime`, and `lifetime_cache_file`.

- [ ] Add concise dated supersession notes to the two executed plans. Do not
  rewrite their old task excerpts or claim that old tests never passed.

- [ ] Add a dated note to the path research stating that the earlier cache
  placement was valid for the former implementation but the corrected feature
  has no Sidekick-owned lifetime cache.

- [ ] Add a dated note to the completion record stating that the architecture
  completion remains historical evidence while the token metric and owner are
  superseded by this plan.

- [ ] Verify every normative cross-reference points only to a tracked file.

- [ ] Run:

  ```bash
  npm run lint:markdown
  uv run pytest tests/test_docs.py tests/test_architecture.py -q
  git diff --check
  ```

**Review gate:** A reader must not find two active design authorities that
recommend different token sources. Historical descriptions remain clearly
marked as superseded behavior.

**Production-valid commit boundary:** This documentation authority may be
committed separately from runtime because it explicitly describes an approved
target whose implementation is pending.

### CS-TA-02: Introduce scope-aware core and usage result models

**Purpose:** Make the truthful states available before implementing either
provider, without preserving the misleading lifetime-output vocabulary.

**Files:**

- Modify: `src/sidekick_usages/core/types.py`
- Modify: `src/sidekick_usages/core/models.py`
- Modify: `src/sidekick_usages/core/__init__.py` if required by local facade
- Modify: `src/sidekick_usages/usage/models.py`
- Modify: `src/sidekick_usages/usage/__init__.py`
- Modify: `tests/test_core_types.py`
- Modify: `tests/test_core_models.py`
- Modify: `tests/test_usage_service.py`

**Steps:**

- [ ] Read all current core dataclasses, type enums, usage failure variants,
  and export conventions.

- [ ] Write focused failing tests for:

  - the two exact `TokenActivityScope` values;
  - valid zero and large integer summaries;
  - rejection of Boolean, negative, and non-integer totals;
  - complete, partial, unavailable, and failed variant invariants; and
  - `UsageCheckResult` preserving one normalized reference time and immutable
    activity outcomes.

  Keep validation assertions parameterized and concise.

- [ ] Run and confirm failure:

  ```bash
  uv run pytest tests/test_core_types.py tests/test_core_models.py tests/test_usage_service.py -q
  ```

- [ ] Add the core enum, summary, explicit unavailable reading, and PEP 695
  union with concise Sphinx docstrings.

- [ ] Add the activity issue vocabulary and closed aggregate variants to
  `usage/models.py`. Validate illegal coverage combinations at construction.

- [ ] Extend `UsageCheckResult` without adding credentials or mutable
  collections.

- [ ] Run:

  ```bash
  uv run ruff format src/sidekick_usages/core src/sidekick_usages/usage/models.py tests/test_core_types.py tests/test_core_models.py tests/test_usage_service.py
  uv run ruff check src/sidekick_usages/core src/sidekick_usages/usage/models.py tests/test_core_types.py tests/test_core_models.py tests/test_usage_service.py
  uv run ty check src/sidekick_usages/core src/sidekick_usages/usage/models.py tests/test_core_types.py tests/test_core_models.py tests/test_usage_service.py
  uv run pytest tests/test_core_types.py tests/test_core_models.py -q
  ```

**Local gate:** The new models may remain uncommitted while the provider and
service migration continues. Do not publish this step as an unused surface.

### CS-TA-03: Implement Claude local activity parity

**Purpose:** Reproduce Claude Code's total and live freshness through a
read-only, bounded provider adapter.

**Files:**

- Create: `src/sidekick_usages/providers/claude/activity.py`
- Modify: `src/sidekick_usages/providers/claude/schemas.py`
- Modify: `src/sidekick_usages/providers/claude/__init__.py`
- Create: `tests/test_claude_activity.py`

**Steps:**

- [ ] Recheck the installed Claude implementation under the section 2.2 gate
  and record only schema and aggregation facts, never transcript content.

- [ ] Write the three load-bearing Claude tests from sections 7.2 through 7.4.
  Use an injected config directory and fixed application clock.

- [ ] Run and confirm failure:

  ```bash
  uv run pytest tests/test_claude_activity.py -q
  ```

- [ ] Add strict private cache and assistant-usage models in Claude schemas.
  Reuse the local `_validate` error translation and safe-field conventions;
  do not expose raw `ValidationError`.

- [ ] Implement `discover_claude_config_dir()` and `ClaudeActivity` in the
  provider activity module. Resolve the environment once during composition,
  then inject the path.

- [ ] Implement bounded regular-file discovery for:

  ```text
  projects/<project>/<session>.jsonl
  projects/<project>/<session>/subagents/agent-*.jsonl
  ```

  Do not use an unconstrained recursive scan that can select tool-result or
  unrelated JSONL files.

- [ ] Implement bounded cache reading, inclusive boundary filtering, parent
  sidechain exclusion, subagent inclusion, cache-token exclusion, snapshot
  EOF handling, and exact input-plus-output aggregation.

- [ ] Return explicit unavailable only for genuine absence. Translate
  malformed and unreadable states to existing secret-safe provider failures.

- [ ] Assert in tests that no provider-owned file changed.

- [ ] Run:

  ```bash
  uv run ruff format src/sidekick_usages/providers/claude tests/test_claude_activity.py
  uv run ruff check src/sidekick_usages/providers/claude tests/test_claude_activity.py
  uv run ty check src/sidekick_usages/providers/claude tests/test_claude_activity.py
  uv run pytest tests/test_claude_activity.py tests/test_core_models.py -q
  ```

**Review gate:** The exact fixture result is `903_464_085`, appending a live
event changes it by the event's non-cached input plus output, and no test
asserts against a private helper merely to inflate coverage.

### CS-TA-04: Implement Codex account-profile activity

**Purpose:** Replace local rollout output with the provider's account lifetime
and reuse the third ChatGPT request call site cleanly.

**Files:**

- Create: `src/sidekick_usages/providers/codex/request.py`
- Create: `src/sidekick_usages/providers/codex/activity.py`
- Modify: `src/sidekick_usages/providers/codex/schemas.py`
- Modify: `src/sidekick_usages/providers/codex/usage.py`
- Modify: `src/sidekick_usages/providers/codex/heartbeat.py`
- Modify: `src/sidekick_usages/providers/codex/__init__.py`
- Create: `tests/test_codex_activity.py`
- Modify: `tests/test_codex_provider.py` only for changed shared-request
  behavior

**Steps:**

- [ ] Recheck the exact upstream release source for the route, header
  construction, profile types, and TUI label.

- [ ] Write the profile, strict-schema, and no-fallback tests from sections
  7.5 through 7.7.

- [ ] Run and confirm failure:

  ```bash
  uv run pytest tests/test_codex_activity.py -q
  ```

- [ ] Extract the exact shared account-id and base-auth-header behavior from
  usage and heartbeat into `request.py`. Do not add configurable methods,
  arbitrary header hooks, or endpoint routing.

- [ ] Update usage and heartbeat to call the helper without changing their
  endpoint-specific media type, beta header, retry class, or behavior.

- [ ] Add strict private profile schemas and a parser that returns only the
  account-scoped `TokenActivitySummary` used by the application.

- [ ] Implement the profile GET through `HttpClient.get_json`. Do not add a
  session, subprocess, app-server client, or filesystem parameter.

- [ ] Verify malformed profile values become safe provider-boundary failures
  and existing HTTP errors retain their typed classes.

- [ ] Run:

  ```bash
  uv run ruff format src/sidekick_usages/providers/codex tests/test_codex_activity.py tests/test_codex_provider.py
  uv run ruff check src/sidekick_usages/providers/codex tests/test_codex_activity.py tests/test_codex_provider.py
  uv run ty check src/sidekick_usages/providers/codex tests/test_codex_activity.py tests/test_codex_provider.py
  uv run pytest tests/test_codex_activity.py tests/test_codex_provider.py tests/test_heartbeat.py -q
  ```

**Review gate:** The exact profile fixture returns `7_449_473_297`, the fake
captures the expected account identity, and no Codex activity code has a
filesystem dependency.

### CS-TA-05: Integrate activity into the usage service

**Purpose:** Produce rows and provider activity from one account selection,
one reference time, and the canonical credential workflow.

**Files:**

- Create: `src/sidekick_usages/usage/activity.py`
- Modify: `src/sidekick_usages/usage/service.py`
- Modify: `src/sidekick_usages/usage/models.py`
- Modify: `src/sidekick_usages/usage/__init__.py`
- Modify: `src/sidekick_usages/cli/context.py`
- Modify: `tests/test_usage_service.py`
- Create: `tests/test_usage_activity.py`
- Modify: `tests/test_support.py`

**Steps:**

- [ ] Add the two structural activity ports to the focused usage activity
  owner consumed by the service. Keep their signatures scope-specific.

- [ ] Update test support to inject local and account activity-source maps.
  Default fakes must fail if an unexpected activity boundary is crossed; do
  not silently return zero.

- [ ] Write or revise the two service tests from section 7.8 and run them to
  confirm failure.

  ```bash
  uv run pytest tests/test_usage_activity.py -q
  ```

- [ ] Refactor the private check outcome to retain the latest trustworthy
  account and explicit activity eligibility alongside either `AccountUsage` or
  `FetchFailure`. Do not infer eligibility later from display-oriented failure
  text.

- [ ] Collect Claude once per selected provider and Codex once per eligible
  checked account, all with the same `reference_time`. Prove that a transient
  rate-limit failure does not suppress an otherwise valid profile read.

- [ ] Map provider boundary and HTTP exceptions explicitly. Use exhaustive
  `isinstance` or `match` handling and `assert_never` for closed states. Do not
  add a blanket catch.

- [ ] Construct complete, partial, unavailable, and failed outcomes according
  to section 4.6. Preserve provider and store order.

- [ ] Compose concrete `ClaudeActivity` and `CodexActivity` instances in
  `compose_app_context`. Remove `LifetimeCollector` from `AppContext`; do not
  leave a compatibility field.

- [ ] Verify that no normal command composes a second `HttpClient` and that
  Claude receives the production config directory without entering
  `ApplicationPaths`.

- [ ] Run:

  ```bash
  uv run ruff format src/sidekick_usages/usage src/sidekick_usages/cli/context.py tests/test_usage_service.py tests/test_support.py
  uv run ruff check src/sidekick_usages/usage src/sidekick_usages/cli/context.py tests/test_usage_service.py tests/test_support.py
  uv run ty check src/sidekick_usages/usage src/sidekick_usages/cli/context.py tests/test_usage_service.py tests/test_support.py
  uv run pytest tests/test_usage_service.py tests/test_cli_persistence.py tests/test_cli_refresh.py -q
  ```

**Review gate:** Usage and activity always describe the same selection;
profile reads use post-refresh accounts; an activity failure cannot remove a
valid usage row; and no new credential mutation path exists.

### CS-TA-06: Render truthful totals and reduce exit state

**Purpose:** Make exact changes visible and partial or local scope impossible
to mistake for complete account lifetime.

**Files:**

- Create: `src/sidekick_usages/usage/activity_render.py`
- Modify: `src/sidekick_usages/usage/render.py`
- Modify: `src/sidekick_usages/cli/commands/usage.py`
- Modify: `tests/test_render.py`
- Modify: `tests/test_check_errors.py`

**Steps:**

- [ ] Replace lifetime imports and fixtures with the new closed activity
  outcomes.

- [ ] Write the wide/narrow rendering contract from section 7.9 and the exit
  behavior from section 7.10. Confirm that the old renderer fails the exact
  strings and scope assertions.

  ```bash
  uv run pytest tests/test_render.py tests/test_check_errors.py -q
  ```

- [ ] Add separate exact and compact token formatters. Keep them private to
  rendering; formatting does not belong in provider or core modules.

- [ ] Replace `_lifetime_text` with an exhaustive activity-outcome renderer.
  Use `known tokens` for partial Codex results and local wording for Claude.

- [ ] Render account-scoped activity issues as concise warning rows beneath
  normal usage content. Authentication warnings include the quoted safe
  `sidekick-usages refresh <label>` command; raw provider messages do not.

- [ ] Change `usage_overview` to receive the completed service result, or an
  equivalently impossible-to-mismatch immutable view. Do not preserve a
  separate lifetime mapping parameter.

- [ ] Keep exact subtitles in panel measurement and the existing 85-column
  framed layout. Use compact values only in the narrow fallback.

- [ ] Simplify the CLI command to call `usage.check`, render once, reduce
  account and activity outcomes to the highest exit code, and exit. Remove all
  provider-id reconstruction and lifetime calls.

- [ ] Run:

  ```bash
  uv run ruff format src/sidekick_usages/usage/render.py src/sidekick_usages/cli/commands/usage.py tests/test_render.py tests/test_check_errors.py
  uv run ruff check src/sidekick_usages/usage/render.py src/sidekick_usages/cli/commands/usage.py tests/test_render.py tests/test_check_errors.py
  uv run ty check src/sidekick_usages/usage/render.py src/sidekick_usages/cli/commands/usage.py tests/test_render.py tests/test_check_errors.py
  uv run pytest tests/test_render.py tests/test_check_errors.py tests/test_cli_branding.py -q
  ```

**Review gate:** Exact wide values visibly change for any token increment;
narrow values match the required precision; partial and local scope are
explicit; profile authentication is actionable without hiding the valid usage
row; and no physical line wraps in the 85-column guard.

### CS-TA-07: Delete the wrong owner and obsolete path contract

**Purpose:** Make the corrected architecture exclusive and remove dead cache,
rollout, path, fixture, and package behavior.

**Files:**

- Delete: `src/sidekick_usages/lifetime.py`
- Delete: `tests/test_lifetime.py`
- Modify: `src/sidekick_usages/paths.py`
- Modify: `tests/test_paths.py`
- Modify: `tests/test_persistence_location_service.py`
- Modify: `tests/test_support.py`
- Modify: `packaging/check_architecture.py`
- Modify: `packaging/smoke_wheel.py`
- Modify any remaining imports found by the exact stale-owner search

**Steps:**

- [ ] Delete the old production and test modules. Do not copy cache parsers or
  rollout helpers into a compatibility file.

- [ ] Remove `lifetime_cache_file` from `ApplicationPaths`, discovery, test
  constructors, location matrices, and assertions.

- [ ] Update architecture checks to require the new provider owners and
  service-owned activity result, while forbidding:

  ```text
  sidekick_usages.lifetime
  LifetimeCollector
  LifetimeTotal
  lifetime_cache_file
  codex-lifetime-cache
  _CODEX_SESSIONS_DIR
  rollout-
  total_token_usage
  ```

  Scope content forbiddance to production owners so historical tracked docs
  can accurately describe the superseded implementation.

- [ ] Update wheel verification to require both activity modules and reject a
  packaged `sidekick_usages/lifetime.py` member.

- [ ] Run the stale-owner search:

  ```bash
  rg -n "from sidekick_usages\.lifetime|import sidekick_usages\.lifetime|LifetimeCollector|LifetimeTotal|lifetime_cache_file|codex-lifetime-cache" src tests packaging README.md docs
  ```

  Remaining hits must be dated historical or migration documentation, never
  active runtime, tests, path construction, or package requirements.

- [ ] Run:

  ```bash
  uv run pytest tests/test_paths.py tests/test_persistence_location_service.py tests/test_architecture.py tests/test_packaging.py -q
  uv run python packaging/check_architecture.py
  ```

**Review gate:** The old metric cannot be imported, instantiated, packaged,
or reconstructed from a current Sidekick path.

### CS-TA-08: Close the atomic runtime migration

**Purpose:** Verify CS-TA-02 through CS-TA-07 as one production-valid runtime
change before any commit.

**Files:** All runtime, test, and packaging files in CS-TA-02 through
CS-TA-07.

**Steps:**

- [ ] Inspect every changed production file for:

  - duplicate helpers;
  - provider leakage into core;
  - usage-model leakage into providers;
  - raw Pydantic or response errors;
  - swallowed `OSError` or JSON failures;
  - `Any`, casts, blanket suppressions, and legacy future annotations;
  - direct wall-clock acquisition;
  - credential or transcript content in repr or messages;
  - hidden provider-state writes; and
  - modules near or above the cohesion threshold.

- [ ] Run the focused feature gate:

  ```bash
  uv run pytest tests/test_claude_activity.py tests/test_codex_activity.py tests/test_core_models.py tests/test_core_types.py tests/test_usage_service.py tests/test_render.py tests/test_check_errors.py tests/test_paths.py tests/test_persistence_location_service.py tests/test_architecture.py tests/test_packaging.py -q
  ```

- [ ] Run the complete static and test gate:

  ```bash
  uv run ruff format --check src/ tests/
  uv run ruff check src/ tests/
  uv run ty check src/ tests/
  uv run python packaging/check_architecture.py
  uv run pytest -q
  git diff --check
  ```

- [ ] Compare the new test inventory with the deleted 12 lifetime cases.
  Remove redundant helper-level tests and explain every retained test by an
  acceptance criterion, not coverage percentage.

- [ ] Inspect the complete runtime diff and confirm `AGENTS.md` remains an
  unstaged, preserved operator change unless separately authorized.

**Production-valid commit boundary:** Only after every step passes may the
runtime, tests, and packaging changes be committed together.

### CS-TA-09: Update active user and operator documentation

**Purpose:** Make public documentation match the now-working product and remove
the obsolete cache from recovery guidance.

**Files:**

- Modify: `README.md`
- Modify: `docs/persistence-and-recovery.md`
- Recheck the Superpowers documents from CS-TA-01
- Modify other active docs only when exact search proves a stale claim

**Steps:**

- [ ] Update README feature copy from local lifetime output totals to:

  - Claude local-installation activity matching Claude `/stats` semantics;
  - Codex saved-account lifetime activity from the provider profile;
  - account versus local scope;
  - known-token partial coverage; and
  - exact wide and compact narrow display behavior.

- [ ] State clearly that Claude's number is not allocated among saved labels.

- [ ] Remove the current lifetime-cache column and recovery instructions from
  persistence documentation. Add one concise note that an older
  `codex-lifetime-cache.json` is inert and may be deleted manually.

- [ ] Do not publish operator-specific totals, account labels, provider
  payloads, or local credential paths.

- [ ] Run:

  ```bash
  npm run lint:markdown
  uv run pytest tests/test_docs.py -q
  git diff --check
  ```

**Production-valid commit boundary:** Public docs may be a separate commit
after the runtime migration is green. They must not describe corrected
behavior before it exists on the publication branch.

### CS-TA-10: Build, package, and perform controlled live parity QA

**Purpose:** Prove the source tree, distributions, and real TUI agree without
turning live credentials into automated test dependencies.

**Files:** No source changes unless a verified defect is found.

**Steps:**

- [ ] Run all final gates in section 12.

- [ ] Build and smoke-test the exact wheel. Confirm both provider activity
  modules are present and the old lifetime module is absent.

- [ ] With explicit operator authorization for live provider reads, run:

  ```bash
  uv run sidekick-usages --only claude
  uv run sidekick-usages --only codex
  ```

  These commands use normal Sidekick behavior and may refresh Sidekick's saved
  credentials when the existing usage service determines that is required.
  They must never modify the active provider login.

- [ ] Compare Claude's displayed exact total with Claude `/stats` at the same
  practical instant. Allow only activity that occurred between the two reads;
  rerun immediately if necessary.

- [ ] Verify the Codex panel equals the sum of successful profile reads across
  saved accounts, including any eligible account whose separate rate-limit
  read failed, and says `known tokens` when activity coverage is incomplete.

- [ ] Capture a redacted before/after terminal image or text for the pull
  request. Remove account identities and recovery tokens.

- [ ] Inspect provider-owned activity files and active login files only by
  metadata if needed; do not copy them into the repository or logs.

**Stop/go gate:** Any mismatch is a defect. Do not explain it away as provider
rounding, local rollouts, cached input, or eventual consistency without direct
evidence from the same authoritative surface.

## 10. Risk controls

| Risk | Required control | Proof |
|---|---|---|
| Claude double-counts the live boundary | Treat the verified cache boundary as inclusive and pin the exact `903_464_085` fixture | Claude parity test |
| Claude appears frozen | Rescan the live suffix on every invocation and append an event in the same test | Claude parity test |
| Cache tokens inflate the total | Validate but exclude both cache fields in cache and transcript records | Large excluded fixture values |
| Parent sidechain copy is counted twice | Exclude parent `isSidechain`; include actual subagent file | Ownership test |
| Active JSONL ends mid-write | Parse a size snapshot and ignore only its final unterminated fragment | Failure-boundary test |
| Transcript content leaks | Generic safe failures; no raw line, path, or payload in repr or display | Error assertions and diff review |
| Claude total is assigned to an account | Local-installation scope has no account label | Model invariant and render test |
| Codex reports local rather than account history | Profile adapter has no filesystem input; architecture forbids rollout terms | Profile and architecture tests |
| Codex account headers drift | Exact route and identity-header assertion against release authority | Profile test and drift gate |
| Daily buckets are mistaken for lifetime | Preserve `lifetime_tokens`; validate but do not sum buckets | Schema test and code review |
| One activity-uncovered account makes the sum look complete | Coverage counts force `PartialTokenActivity` and `known tokens` | Service and render tests |
| Profile failure erases a rate-limit row | Activity is a parallel result inside the completed usage result | Service and CLI tests |
| Profile `401` triggers duplicate refresh | No refresh in activity collection; canonical usage path owns refresh | Service test and call review |
| Exact subtitle causes wrapping | Retain the 85-column physical-line guard | Render test |
| Old cache silently returns | Delete module and path; package and architecture forbiddance | Architecture and wheel tests |
| Provider login is modified | Read Claude state only; use saved Codex account HTTP; no app-server auth homes | Adapter API review and live QA |
| A new dependency duplicates owned behavior | Lockfile remains unchanged and existing boundaries are reused | Diff review and build |
| Historical docs become misleading | Active specs corrected; historical records receive dated notes | Markdown and docs review |

## 11. Explicit non-goals and prohibited shortcuts

### 11.1 Non-goals

This plan does not add:

- per-account attribution for Claude local history;
- cached-input token counts to the user-facing total;
- a local Codex rollout or SQLite activity mode;
- a new public command, JSON schema, daemon task, or configuration setting;
- a daily activity chart, peak, streak, or longest-turn panel;
- automatic deletion of an inert cache from an older Sidekick release;
- an app-server subprocess manager;
- a Node runtime or third-party usage parser;
- a second HTTP or retry stack;
- concurrent provider requests; or
- an automatic edit of provider login or activity files.

These are deliberate scope boundaries. Optional Codex profile fields are
validated now so malformed external data cannot hide behind an unused field,
but they are not promoted into public domain models until a separately
approved product surface consumes them.

### 11.2 Prohibited shortcuts

Do not:

- change only Claude `outputTokens` to input plus output and leave freshness,
  scope, Codex source, formatting, and ownership defects in place;
- rename `LifetimeTotal.output_tokens` while retaining the cross-provider
  collector;
- parse Claude's interactive text output;
- invoke Codex app-server against a saved private credential directory;
- stage a temporary Codex `auth.json`;
- use local rollouts when the Codex profile is unavailable;
- use the sum of returned daily buckets as account lifetime;
- represent unavailable, malformed, or partial state as zero;
- catch `Exception` or `OSError` and continue with a plausible default;
- add an `Any`, cast, blanket suppression, or untyped fake to make the gate
  pass;
- add a general activity framework with speculative providers or options;
- copy the robot, renderer, HTTP client, account-id logic, clock, or JSON
  decoder into a new owner;
- retain cache tests after deleting the cache behavior;
- snapshot the full TUI to inflate coverage;
- weaken an architecture, typing, Markdown, package, or compatibility gate;
- commit the atomic runtime migration in partially wired pieces; or
- include credentials, account identities, transcripts, or provider response
  bodies in fixtures, documentation, logs, or screenshots.

## 12. Acceptance traceability

| ID | Acceptance criterion | Owning change set | Load-bearing proof |
|---|---|---|---|
| AC-01 | Claude cache input plus output is included | CS-TA-03 | Exact parity fixture |
| AC-02 | Claude live activity increments without cache rewrite | CS-TA-03 | Append-and-reread assertion |
| AC-03 | Claude cache-read and creation tokens are excluded | CS-TA-03 | Large excluded fields |
| AC-04 | Parent sidechain copy is excluded and subagent included | CS-TA-03 | Transcript ownership test |
| AC-05 | Claude absence, malformed input, and active EOF differ | CS-TA-03 | Failure-boundary test |
| AC-06 | Claude is local-installation scoped | CS-TA-02, CS-TA-05 | Model and service tests |
| AC-07 | Codex exact lifetime comes from account profile | CS-TA-04 | `7_449_473_297` profile fixture |
| AC-08 | Codex route and account identity are exact | CS-TA-04 | Capturing fake HTTP assertion |
| AC-09 | Codex rejects missing, Boolean, negative, and overflow totals | CS-TA-04 | Strict schema parameterization |
| AC-10 | No Codex local fallback exists | CS-TA-04, CS-TA-07 | Failure and architecture tests |
| AC-11 | Every eligible Codex account is read independently and successful profiles sum exactly | CS-TA-05 | Service aggregation test |
| AC-12 | Incomplete Codex coverage says `known tokens` | CS-TA-05, CS-TA-06 | Partial service and render tests |
| AC-13 | Activity failure retains valid usage rows | CS-TA-05, CS-TA-06 | Service and CLI tests |
| AC-14 | Wide values are exact and narrow values remain precise | CS-TA-06 | Parameterized render contract |
| AC-15 | The 85-column framed TUI does not wrap | CS-TA-06 | Existing width guard extended |
| AC-16 | Old collector and cache cannot ship | CS-TA-07 | Architecture and wheel gates |
| AC-17 | No new dependency or retry owner exists | CS-TA-04, CS-TA-08 | Lockfile and architecture review |
| AC-18 | Active docs tell the corrected scope and source truth | CS-TA-01, CS-TA-09 | Markdown and docs tests |
| AC-19 | Provider active logins and state are not modified | CS-TA-03, CS-TA-04, CS-TA-10 | Adapter contract and controlled QA |
| AC-20 | Full source, package, and compatibility gates pass | CS-TA-08, CS-TA-10 | Final verification |

No acceptance criterion is satisfied by coverage percentage alone.

## 13. Final specification-parity sweep

Before final verification, compare the implementation line by line against
sections 1, 4, 6, 7, 10, and 12. Record pass or corrective action for every
item below:

- [ ] `TokenActivityScope` has exactly account and local-installation values.
- [ ] `TokenActivitySummary.total_tokens` is strict and non-negative.
- [ ] Absence is explicit and distinct from valid zero.
- [ ] Aggregate states distinguish complete, partial, unavailable, and failed.
- [ ] Claude resolves the provider config directory correctly.
- [ ] Claude cache total uses input plus output.
- [ ] Claude live boundary is inclusive and UTC-based.
- [ ] Claude cache-read and cache-creation counts are excluded.
- [ ] Claude parent sidechain copies are excluded.
- [ ] Claude actual subagent events are included.
- [ ] Claude active final fragments are handled without hiding complete
  malformed records.
- [ ] Claude state is never written.
- [ ] Claude result has local scope and no saved label.
- [ ] Codex calls the exact account-profile route through shared HTTP.
- [ ] Codex sends the saved account identity.
- [ ] Codex lifetime field is strict and authoritative.
- [ ] Codex buckets are validated but not substituted for lifetime.
- [ ] Codex profile is read independently for each eligible saved account,
  including one with a non-authentication rate-limit-read failure.
- [ ] Codex never reads rollouts, SQLite, or the old cache.
- [ ] A profile `401` does not start a second refresh loop.
- [ ] Complete and partial aggregation follow selected-account coverage.
- [ ] Activity and rate-limit rows use one selection and reference time.
- [ ] Wide rendering is exact and narrow rendering matches required precision.
- [ ] Claude local and Codex partial wording is explicit.
- [ ] `output` is absent from the corrected activity display.
- [ ] The old module, path field, cache implementation, and tests are gone.
- [ ] Architecture and wheel gates require the new ownership.
- [ ] README, persistence docs, active specs, and historical notes agree.
- [ ] No new dependency, setting, command, or provider-state mutation landed.
- [ ] Every added test protects a named acceptance criterion.

Do not proceed to completion while any box is unresolved.

## 14. Final verification

Run from the repository root in this order.

### 14.1 Environment and locked dependency gate

```bash
uv sync --all-groups
npm ci
npm audit --audit-level=moderate
```

Expected: the lockfiles remain unchanged unless a separately approved tool
update is required. This feature adds no dependency.

### 14.2 Focused feature gate

```bash
uv run pytest \
  tests/test_claude_activity.py \
  tests/test_codex_activity.py \
  tests/test_core_models.py \
  tests/test_core_types.py \
  tests/test_usage_service.py \
  tests/test_render.py \
  tests/test_check_errors.py \
  tests/test_paths.py \
  tests/test_persistence_location_service.py \
  tests/test_architecture.py \
  tests/test_packaging.py \
  -q
```

Expected: all focused cases pass, including exact `903_464_085` and
`7_449_473_297` assertions.

### 14.3 Static and architecture gate

```bash
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run ty check src/ tests/
uv run python packaging/check_architecture.py
```

Expected: all gates pass. Only previously accepted cohesion warnings may
remain, and no changed module may add a warning without an explicit split or
reviewed justification.

### 14.4 Full behavior and compatibility gate

```bash
uv run pytest -q
uv run pytest --cov=sidekick_usages
uv run pytest tests/test_v060_compat.py tests/test_v060_runtime.py -q
```

Expected: all applicable tests pass and platform-only cases remain explicit
skips on Linux. Coverage is diagnostic; it is not a reason to add filler tests.

### 14.5 Documentation and repository gate

```bash
npm run lint:markdown
uv run pre-commit run --all-files
git diff --check
```

Expected: all documentation and repository hooks pass without suppression.

### 14.6 Stale-owner and safety sweep

```bash
rg -n "from sidekick_usages\.lifetime|import sidekick_usages\.lifetime|LifetimeCollector|LifetimeTotal|lifetime_cache_file|_CODEX_SESSIONS_DIR|codex-lifetime-cache" src tests packaging README.md docs
rg -n "output tokens|output-token|output only|output-only" src tests README.md docs
rg -n "Any|cast\(|# noqa|# type: ignore|# nosec|from __future__ import annotations" src/sidekick_usages/providers/claude/activity.py src/sidekick_usages/providers/codex/activity.py src/sidekick_usages/providers/codex/request.py src/sidekick_usages/usage
```

Expected:

- no active production or test owner refers to the old collector or cache;
- any old terms in historical docs are inside dated supersession context;
- no new type escape or suppression exists; and
- no corrected user-facing copy calls total activity `output`.

Review the complete diff for tokens, email addresses, account ids, project
names, response bodies, transcript content, and local provider payloads.

### 14.7 Distribution gate

```bash
uv build
uv run python packaging/smoke_wheel.py --build
```

Expected:

- wheel and source distribution have exact declared names;
- both provider activity modules are present;
- `sidekick_usages/lifetime.py` is absent;
- `sidekick-usages -h` and packaged smoke commands pass; and
- the wheel does not contain local caches, credentials, transcripts, or build
  artifacts.

### 14.8 Controlled product proof

After automated gates, perform CS-TA-10 only with live-read authorization.
Capture the exact terminal widths used and redact account identities. Verify:

- a wide Claude panel shows an exact integer and local scope;
- immediate new Claude activity becomes visible without waiting for cache
  refresh;
- a wide Codex panel shows the exact successful profile sum across every
  eligible saved account;
- an activity-uncovered Codex account changes the aggregate wording to
  `known tokens`;
- the narrow fallback uses `903.46M` and `7.449B`-style precision; and
- the active Claude and Codex login files are unchanged by Sidekick.

Cross-platform CI remains required before release because filesystem and Rich
behavior must pass Linux, macOS, and Windows even though local parity QA runs
on the audited Linux environment.

## 15. Recommended commit sequence

Commit only when requested by the operator.

1. `docs(design): correct token activity architecture`

   Include this plan, active specification corrections, and dated historical
   supersession notes. Do not include runtime behavior or the existing
   `AGENTS.md` change.

2. `fix(usage): use authoritative provider token activity`

   Include the complete atomic runtime migration, concise tests, path removal,
   architecture checks, and wheel contract from CS-TA-02 through CS-TA-08.

3. `docs: document token activity scope and sources`

   Include README and persistence documentation after the runtime behavior is
   present.

4. Commit the existing `AGENTS.md` refresh separately only when the operator
   explicitly requests it and its own documentation gates remain green.

Before every commit:

```bash
git status --short
git diff --check
git diff --cached --stat
git diff --cached
```

Confirm the staged set matches exactly one boundary and contains no provider
identity or secret. Push only after explicit authorization.

## 16. Definition of complete

The plan is complete only when all of the following are true:

- all 20 acceptance criteria pass;
- the final specification-parity sweep has no unresolved item;
- automated final verification is green;
- controlled live QA either passes or is explicitly waived by the operator;
- the old collector and cache cannot execute or ship;
- active docs describe the implemented behavior exactly;
- historical documents clearly identify the superseded metric;
- no credentials, identities, provider payloads, or local-only references are
  present in tracked changes;
- the existing `AGENTS.md` change is preserved and scoped according to the
  operator's instruction;
- no commit or push occurs without authorization; and
- the handoff reports exact files, tests, gate results, live-QA status, commit
  ids when applicable, and any remaining external CI requirement.

There is no acceptable partial completion in which Claude is corrected but
Codex remains local, Codex is corrected but Claude remains stale, the numbers
are correct but scope is misleading, or runtime is correct while the old cache
and active documentation remain authoritative.

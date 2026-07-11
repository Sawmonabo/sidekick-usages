# Durable Token Activity Snapshots Implementation Plan

> **For agentic workers:** Execute this plan in order. Preserve the strict
> persistence boundary, authoritative-source rules, account-identity safety,
> and verification gates when the execution mechanism changes.

- **Status:** Implemented and verified
- **Date:** 2026-07-11
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Branch:** `develop`
- **Baseline commit:**
  `ff9e632d12f6683f682b0fe5786c7c9263bdc6ea`
- **Upstream:** `origin/develop`
- **Provider baseline:** Claude Code `2.1.207`; Codex CLI `0.144.1`
- **Codex source commit:**
  `44918ea10c0f99151c6710411b4322c2f5c96bea`
- **Implementation authority:** This plan and the primary provider sources
  linked in section 2

> **Presentation supersession (2026-07-11):** The completed
> [token start year and narrow-layout plan][token-start-year-plan] adds the
> four-digit year and deliberate two-line narrow presentation. This document
> remains authoritative for snapshot and provider-accounting behavior.

## 1. Outcome

Both provider panels use one stable token-activity footer contract:

```text
╰────────────────── 915,947,703 tokens  ·  since Dec 28, 2025 ─╯
```

The implementation must provide all of the following:

- remove `local`, `local CLI`, and `known tokens` from token-activity output;
- render the metric as `tokens` for Claude and Codex in complete and partial
  states;
- render `since <date>` for both providers whenever the authoritative source
  or a validated authoritative snapshot proves the date;
- keep authentication and refresh failures in their existing account rows;
- retain the last successful authoritative Codex account total when a later
  profile request fails;
- update a Codex account snapshot after every successful profile response;
- derive a Codex start date only when the returned daily buckets are present,
  nonempty, unique, valid, nonnegative, and sum exactly to
  `lifetime_tokens`;
- aggregate fresh and retained per-account snapshots without substituting
  rollout, SQLite, credential, or guessed values;
- preserve explicit failure behavior when no authoritative snapshot exists;
- never mutate provider-owned activity state or the active provider login;
  and
- add no runtime dependency.

This plan supersedes the presentation and no-snapshot fallback decisions in
the completed token-activity accuracy plan. That plan remains the authority
for Claude parity, Codex profile selection, HTTP behavior, and removal of the
incorrect rollout calculation.

## 2. Ground truth

### 2.1 Codex profile behavior

The exact installed Codex release requires ChatGPT authentication and fetches
the backend token-usage profile for every `account/usage/read` request:
[Codex account request processor][codex-account-processor].

The official app-server surface describes `account/usage/read` as fetching an
account token-activity summary and daily buckets:
[Codex app-server account API][codex-app-server].

The exact protocol contains `lifetimeTokens` in the summary and optional daily
buckets with `startDate` and `tokens`. It does not expose a dedicated first
activity or lifetime-start field:
[Codex account protocol][codex-account-protocol].

The provider response observed on 2026-07-11 contained 65 unique daily buckets
from 2026-04-07 through 2026-07-10. Their nonnegative token sum exactly equaled
the response's authoritative `lifetime_tokens`. A second saved account had a
future JWT expiry timestamp but the provider rejected its access token. This
proves that provider authentication results, not embedded expiry alone, govern
fresh profile availability.

### 2.2 Local-state boundary

The credential document contains access, refresh, account-identity, expiry,
refresh, and heartbeat state. It does not contain lifetime token activity.
OAuth credentials authorize a profile request; they do not encode the usage
profile.

The retired `codex-lifetime-cache.json` contains the old local-rollout output
calculation. Codex rollout files and SQLite state do not represent the account
profile's `lifetime_tokens` population and must never seed or repair the new
snapshot authority.

Claude already has durable provider-owned history in `stats-cache.json` plus
its live transcript suffix. Sidekick does not create a second Claude snapshot.

## 3. Product contract

### 3.1 Normal provider footer

Wide panels render exact grouped integers and a short date:

```text
915,947,703 tokens  ·  since Dec 28, 2025
7,455,971,162 tokens  ·  since Apr 7, 2026
```

The narrow fallback retains compact precision and the same date contract:

```text
CLAUDE · 915.95M tokens
         since Dec 28, 2025

CODEX · 7.456B tokens
        since Apr 7, 2026
```

The renderer never emits `local`, `local CLI`, `known token`, or
`known tokens`.

### 3.2 Authentication failure with a snapshot

When a Codex account profile request cannot be attempted or fails but a valid
snapshot exists:

1. include the snapshot's total and verified date in the provider aggregate;
2. preserve the existing authentication or refresh warning in the account
   row;
3. do not replace, delete, or downgrade the snapshot; and
4. keep the footer's standard `tokens · since` wording.

The warning communicates that the retained value could not be refreshed. The
footer remains stable and does not rename the metric.

### 3.3 Failure without a snapshot

An account that has never produced a successful authoritative profile has no
recoverable lifetime total. Sidekick must not infer one from credentials,
rollouts, SQLite, daily data belonging to another account, or panel history.

The existing account failure remains visible. Any aggregate contains only
accounts backed by a fresh response or valid snapshot. The metric still says
`tokens`; the account rows disclose missing coverage.

### 3.4 Date semantics

`since` means the earliest verified activity date represented by the displayed
token total:

- Claude uses `firstSessionDate` from its validated provider-owned cache;
- Codex uses the earliest daily bucket only when all buckets pass validation
  and their sum equals `lifetime_tokens`;
- a Codex response with absent, empty, or non-reconciling buckets has no newly
  verified date;
- a prior verified Codex date may be retained when a newer total omits buckets
  and does not regress below the prior total;
- a newer authoritative response that proves its own date replaces the prior
  date; and
- a provider aggregate uses the minimum date only when every included account
  total has a verified date.

No local rollout filename, account save time, JWT time, first Sidekick
observation, or bounded non-reconciling bucket date may be presented as
`since`.

## 4. Architecture

### 4.1 Shared domain model

Add `AccountTokenActivitySnapshot` to `core/models.py` with:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class AccountTokenActivitySnapshot:
    provider_id: ProviderId
    provider_account_id: str = field(repr=False)
    summary: TokenActivitySummary
    fetched_at: datetime
```

The model must:

- require a nonempty provider account identity;
- require `summary.scope is TokenActivityScope.ACCOUNT`;
- normalize `fetched_at` to aware UTC;
- retain the account identity outside representations; and
- reuse `TokenActivitySummary` validation for totals and dates.

Do not add raw buckets, freshness flags, labels, credentials, or speculative
provider fields to the shared model.

### 4.2 Snapshot persistence owner

Add `persistence/activity_snapshots.py` as the sole Sidekick snapshot owner.
It owns:

- the strict versioned Pydantic document schema;
- deterministic canonical JSON encoding;
- duplicate-key and malformed-JSON rejection;
- stable account-identity key derivation;
- bounded reads;
- qualified filesystem access;
- hard-lock coordination;
- atomic replacement and post-write durability proof; and
- typed, secret-safe snapshot failures.

The production file is discovered only by `paths.py` and lives at:

```text
<native application data>/token-activity.json
```

It is separate from `accounts.json`, private Codex credential homes, provider
state, and the retired compatibility cache.

### 4.3 Persisted document

The initial schema is conceptually:

```json
{
  "schema_version": 1,
  "accounts": {
    "<sha256 provider/account identity>": {
      "provider_id": "codex",
      "total_tokens": 7449473297,
      "since": "2026-04-07",
      "fetched_at": "2026-07-11T04:30:00Z"
    }
  }
}
```

Requirements:

- never persist raw account labels, access tokens, refresh tokens, ID tokens,
  raw provider account IDs, HTTP payloads, or provider error text;
- derive the key from the provider ID plus stable provider account ID using
  SHA-256 and an unambiguous separator;
- require exactly 64 lowercase hexadecimal key characters;
- allow only supported provider IDs;
- bound totals to signed 64-bit nonnegative integers;
- use canonical ISO dates and aware UTC timestamps;
- reject unknown fields, duplicate members, invalid types, invalid values,
  unsupported schema versions, and oversized documents; and
- limit the number of records even when the byte bound would allow more.

Raw daily buckets are not persisted. Once reconciliation proves the earliest
date, the total, verified date, and fetch time are the minimum durable state
needed by the product. Persisting the full provider response would increase
private metadata and migration surface without a consumer.

### 4.4 Snapshot update policy

Under the snapshot-file lock:

1. bounded-read and strictly decode the current document, or start with an
   empty document when absent;
2. locate the stable identity digest;
3. reject an older incoming `fetched_at` by retaining the newer stored record;
4. accept an identical equal-time write idempotently;
5. reject conflicting equal-time data explicitly;
6. use a newly verified incoming `since` date when present;
7. when the incoming date is absent, preserve the old verified date only if
   the incoming total is not less than the stored total;
8. replace a regressed total as the newer provider authority but do not carry
   an unproven old date into it;
9. encode the complete merged document canonically;
10. atomically commit against the exact observed fingerprint; and
11. return the exact durable snapshot used by the caller.

A failed fetch never calls the update path. A failed update never destroys the
old file and does not suppress the fresh in-memory total; it produces an
explicit persistence issue and a nonzero system result.

### 4.5 Application integration

Add an injected `AccountTokenActivitySnapshots` protocol to
`usage/activity.py`. Production composition supplies the strict persistence
store; tests may supply focused fakes.

For each selected account:

1. use the post-usage-check account copy when it contains provider-discovered
   durable identity;
2. if fresh activity is eligible, request the provider profile;
3. validate the reading's account scope;
4. save a successful summary and use the durable returned snapshot;
5. on a provider failure or unavailable reading, load the prior snapshot;
6. for an account ineligible for fresh activity, load the prior snapshot
   without making another provider request;
7. include every fresh or snapshot-backed total in the aggregate;
8. retain account-aligned activity issues for failed fresh requests or
   persistence operations; and
9. never treat missing snapshot state as a numeric zero.

`CompleteTokenActivity` gains an `issues` tuple because numeric account
coverage can be complete while one or more values could not be refreshed.
`activity_has_failure` and warning rendering must honor those issues.

Add `PERSISTENCE` to `TokenActivityFailureKind`. Persistence failures receive
specific secret-safe presentation copy and remain system errors.

### 4.6 Provider parsing

Update the Codex profile parser to:

1. retain the authoritative `lifetime_tokens` field;
2. validate every optional bucket as it does today;
3. reject duplicate dates;
4. sum bucket tokens with an explicit signed-64-bit boundary;
5. return `since=min(bucket_dates)` only when a nonempty bucket set sums
   exactly to `lifetime_tokens`; and
6. otherwise return the authoritative total with `since=None`.

A non-reconciling optional bucket list does not invalidate the authoritative
lifetime total. It only fails to prove the date.

### 4.7 Composition and paths

Extend `ApplicationPaths` with one canonical snapshot file path. Production
composition constructs one `ActivitySnapshotStore` and injects it into
`UsageCheckService`. No command performs eager snapshot mutation during
composition.

The account credential schema and released rollback contract remain
unchanged. Deleting or renaming an account cannot cause another account's
snapshot to be selected because lookup uses stable provider identity rather
than the label.

## 5. Error and recovery matrix

| Situation | Numeric result | Snapshot action | User-visible state |
|---|---|---|---|
| Fresh profile succeeds | Fresh total | Durable replace | Normal footer |
| Fresh succeeds, save fails | Fresh total | Old file preserved | Footer plus persistence warning |
| Auth fails, snapshot exists | Snapshot total | No write | Footer plus account auth warning |
| Transient profile fails, snapshot exists | Snapshot total | No write | Footer plus activity warning |
| Fresh unavailable, snapshot exists | Snapshot total | No write | Normal or explicit source warning |
| Failure, no snapshot | No value for account | No write | Existing account/activity failure |
| Snapshot malformed | No snapshot fallback | No overwrite | Explicit persistence failure |
| Snapshot unreadable/unsafe | No snapshot fallback | No overwrite | Explicit persistence failure |
| Fresh total regresses | New fresh total | Replace; drop unproven old date | Explicit current authority |
| Older concurrent result arrives | Newer stored total | Retain newer record | No regression |
| Equal-time conflicting result | Fresh in-memory total | Reject conflicting write | Persistence warning |

## 6. Build-versus-adopt decision

Do not add a dependency.

The repository already depends on and uses:

- Pydantic for strict external and persisted schema validation;
- the strict duplicate-rejecting JSON decoder;
- `PersistenceFilesystem` for bounded, permission-qualified, atomic,
  durability-proven private files;
- `PersistenceLock` for cross-process coordination;
- SHA-256 content and identity helpers;
- `pathlib`, `date`, and aware UTC `datetime` values; and
- the existing typed usage and persistence error vocabularies.

Adding a general cache library, database, ORM, retry package, or key-value
store would duplicate these guarantees and increase packaging, migration, and
security maintenance. The new code should compose existing repository
primitives rather than reimplement native atomic writes or locking.

## 7. File-level implementation sequence

### Task 1: Domain and path contracts

Modify:

- `src/sidekick_usages/core/models.py`
- `src/sidekick_usages/core/__init__.py`
- `src/sidekick_usages/paths.py`
- `tests/test_core_models.py`
- `tests/test_paths.py`
- shared path fixtures in `tests/test_support.py`

Prove the snapshot model invariants and canonical path without adding
behavioral padding.

### Task 2: Strict snapshot persistence

Create:

- `src/sidekick_usages/persistence/activity_snapshots.py`
- `tests/test_activity_snapshots.py`

Test only load-bearing behavior:

1. two account snapshots survive a canonical round trip and the document
   contains no labels, raw account IDs, or credentials;
2. an authentication-era load returns the exact last successful total and
   date;
3. newer-write, older-write, idempotent, date-preservation, and regression
   policy cannot overwrite newer truth incorrectly; and
4. malformed state fails closed and is not overwritten.

Do not retest native atomic-write mechanics already covered by persistence
filesystem and locking suites.

### Task 3: Codex verified date

Modify:

- `src/sidekick_usages/providers/codex/schemas.py`
- `tests/test_codex_activity.py`

Test one reconciling response, one non-reconciling response, and the existing
malformed/duplicate boundaries. Do not add one test per private helper.

### Task 4: Fresh-or-snapshot collection

Modify:

- `src/sidekick_usages/usage/activity.py`
- `src/sidekick_usages/usage/models.py`
- `src/sidekick_usages/usage/service.py`
- `src/sidekick_usages/usage/__init__.py`
- `src/sidekick_usages/cli/context.py`
- `tests/test_usage_activity.py`
- relevant composition tests

Prove:

1. fresh reads are durably captured;
2. an auth-ineligible account contributes its prior snapshot without another
   HTTP profile call;
3. a fresh profile failure falls back to its snapshot and keeps the warning;
4. no snapshot means no fabricated account value;
5. persistence failure keeps the fresh value but produces a system issue; and
6. fresh and retained accounts aggregate with the minimum verified date.

### Task 5: Stable footer presentation

Modify:

- `src/sidekick_usages/usage/activity_render.py`
- `src/sidekick_usages/usage/render.py` only if layout requires it
- `tests/test_render.py`
- `tests/test_check_errors.py`

Use exact contract assertions because the footer copy is an intentional
product interface. Assert the absence of `local` and `known tokens`.

### Task 6: Architecture, packaging, and documentation

Modify only the active contracts affected by the feature:

- `README.md`
- `docs/persistence-and-recovery.md`
- active Superpowers architecture and TUI design documents
- the completed token-activity plan with a concise supersession pointer
- `packaging/check_architecture.py` and its tests when ownership needs a gate
- `packaging/smoke_wheel.py` and packaging tests for the new module

Do not reference ignored evidence or local account data. Do not document raw
account identities or live credential details.

## 8. Acceptance criteria

- **AC-01:** Claude renders `<exact tokens> tokens · since <date>`.
- **AC-02:** Codex renders `<exact tokens> tokens · since <date>` when the
  aggregate date is verified.
- **AC-03:** No normal or narrow activity output contains `local`,
  `local CLI`, `known token`, or `known tokens`.
- **AC-04:** A successful Codex profile persists its exact account total,
  verified date, and fetch time under stable hashed identity.
- **AC-05:** A later authentication failure reuses the prior snapshot and
  retains the account warning.
- **AC-06:** A never-successful account receives no fabricated total.
- **AC-07:** Codex derives `since` only from a nonempty, valid, unique,
  nonnegative bucket set whose sum equals `lifetime_tokens`.
- **AC-08:** A non-reconciling bucket list preserves the authoritative total
  but does not invent a date.
- **AC-09:** Snapshot reads and writes are bounded, strict, locked, atomic,
  durability-proven, secret-safe, and fail closed.
- **AC-10:** Account labels and raw provider account IDs do not appear in the
  snapshot document.
- **AC-11:** Newer snapshots cannot be overwritten by older concurrent
  results.
- **AC-12:** The credential schema, active provider login, provider-owned
  files, and retired rollout cache remain untouched.
- **AC-13:** Fresh and retained account values aggregate without overflow and
  use the minimum date only when every included value has a verified date.
- **AC-14:** Focused tests, full tests, static gates, architecture checks,
  documentation lint, and exact wheel verification pass.

## 9. Verification sequence

Run focused checks after each owner changes:

```bash
uv run pytest tests/test_core_models.py tests/test_paths.py
uv run pytest tests/test_activity_snapshots.py
uv run pytest tests/test_codex_activity.py
uv run pytest tests/test_usage_activity.py
uv run pytest tests/test_render.py tests/test_check_errors.py
```

Run owner-level static checks:

```bash
uv run ruff check src/sidekick_usages tests/
uv run ruff format --check src/sidekick_usages tests/
uv run ty check src/sidekick_usages tests/
uv run python packaging/check_architecture.py
```

Run the complete repository gates:

```bash
uv run pytest --cov=sidekick_usages
uv run pre-commit run --all-files --show-diff-on-failure
npm run lint:markdown
npm audit --audit-level=moderate
uv run python packaging/smoke_wheel.py --build
```

Perform a final read-only runtime check on `develop`:

```bash
COLUMNS=120 NO_COLOR=1 uv run sidekick-usages
```

Verify that:

- both provider footers say `tokens · since`;
- neither footer says `local` or `known tokens`;
- a valid Codex response writes the canonical snapshot;
- a subsequent rejected profile can reuse a prior snapshot;
- account warnings remain visible;
- no active provider login or provider-owned file was modified;
- `git diff --check` passes; and
- the worktree contains only intentional implementation and documentation
  changes.

Do not commit or push unless the operator explicitly requests it.

## 10. Implementation verification record

Implementation completed on `develop` on 2026-07-11. The final repository
state satisfies AC-01 through AC-14.

The live working-tree CLI rendered the approved provider-neutral contract:

```text
917,064,538 tokens  ·  since Dec 28, 2025
7,486,342,730 tokens  ·  since Apr 7, 2026
```

The account authentication warning remained visible. The rejected account had
never produced a successful profile after this feature was introduced, so no
authoritative snapshot existed for it and no value was fabricated. The valid
profile produced one strict account-scoped snapshot. The snapshot file was
mode `0600`, contained one SHA-256-keyed record, and contained no raw account
label or provider account ID. The behavior test suite proves that this snapshot
is retained and aggregated if a later profile request fails, without mutating
credentials to manufacture that condition during live verification.

Final gates:

- `uv run pytest --cov=sidekick_usages`: 804 passed, four platform-specific
  skips;
- Ruff lint and format checks: passed;
- `uv run ty check src tests`: passed;
- architecture contracts: passed with the same eight pre-existing cohesion
  warnings;
- all pre-commit hooks, including security and vulnerability checks: passed;
- Markdown lint and `npm audit --audit-level=moderate`: passed; and
- exact built-wheel verification: passed for
  `sidekick_usages-0.6.0-py3-none-any.whl`.

No commit or push was performed as part of this implementation request.

[codex-account-processor]:
  https://github.com/openai/codex/blob/44918ea10c0f99151c6710411b4322c2f5c96bea/codex-rs/app-server/src/request_processors/account_processor.rs#L991-L1015
[codex-app-server]:
  https://github.com/openai/codex/blob/44918ea10c0f99151c6710411b4322c2f5c96bea/codex-rs/app-server/README.md#L1926-L1937
[codex-account-protocol]:
  https://github.com/openai/codex/blob/44918ea10c0f99151c6710411b4322c2f5c96bea/codex-rs/app-server-protocol/src/protocol/v2/account.rs#L387-L443
[token-start-year-plan]: ./2026-07-11-token-start-year-and-narrow-layout.md

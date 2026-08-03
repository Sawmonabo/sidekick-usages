# Hardened Global Account Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a height-responsive, saved-account-only dashboard; enable
only qualified provider selection; and keep Claude setup/mixed switching
visibly unavailable until its protected-plane release gates prove seamless
next-turn adoption without interrupting provider work.

**Architecture:** Keep one provider-neutral, non-secret selection-epoch
coordinator in the existing per-user supervisor. Refreshable Claude uses the
official native transaction. A release-disabled Claude prototype carries one
operation-scoped lease from the isolated worker through the existing exchange
and a provider-owned capability socket to an unchanged structured engine;
install receipt, READY, and genuine-turn adoption remain distinct. Enrolled
Codex TUIs use a narrow admission relay in front of the existing resident app
server, whose Responses provider is direct HTTP and resolves current external
auth for every attempt. Provider-owned processes remain the only durable
credential writers, while prompt-toolkit is the only dashboard painter.

**Tech Stack:** Python 3.14, Typer, prompt-toolkit 3.0.52, Rich semantic
rendering, Pydantic 2.13.4, websockets 16.1.1, portalocker 3.2.0, strict JSON
and framed Unix sockets, pytest 9, Unix pseudoterminals, Ruff, `ty`, `uv`,
systemd user services, LaunchAgents, Bash, Zsh, and Fish.

## Global Constraints

- The user approved the [normative design][approved-design] on 2026-08-01.
- This plan supersedes conflicting behavior in the 2026-07-23 and 2026-07-26
  selection plans. It reuses their shipped foundations and does not re-create
  private authorities, maintenance workers, usage collection, or app-server
  external auth.
- Create all implementation commits on
  `feat/hardened-global-account-selection`, based on the final plan commit at
  `origin/develop`. Do not implement on `develop`.
- Use `superpowers:using-git-worktrees` before creating the implementation
  worktree. Do not create the worktree while publishing this plan.
- Keep `/home/sabossedgh/.local/bin/sidekick-usages` installed at version
  `0.7.0` and SHA-256
  `0b6d2359a50e6a8d19605ea01d7c666c09b87b665709b2c1818161defcd1d966`
  throughout feature work. Do not install an editable checkout or feature
  wheel over it before final cutover.
- The installed command remains the live metrics-reporting path during every
  feature task. Development commands use `uv run` only with synthetic tests or
  isolated temporary application paths; they never point at the user's live
  Sidekick or provider state.
- On this WSL host, the inherited environment currently omits
  `WSL_DISTRO_NAME`; unqualified 0.7.0 reporting reaches the known platform
  composition error. Until the final qualified cutover fixes that source bug,
  the verified process-local compatibility command is
  `env WSL_DISTRO_NAME=Ubuntu sidekick-usages --no-interactive check`.
  It reports metrics successfully without changing the installed executable,
  shell files, provider credentials, daemon configuration, or saved accounts.
- No task copies, edits, replaces, or rolls back a provider credential file.
  Official Claude and Codex processes remain the only durable credential
  writers.
- Persist only provider ID, stable saved-account ID, monotonically increasing
  epoch, authority generation, timestamps, safe outcomes, and bounded opaque
  participant IDs. Never persist access tokens, refresh tokens, prompts,
  responses, provider payloads, emails, or process command lines.
- A turn, retry, tool, hook, approval, MCP operation, or realtime conversation
  admitted under epoch N finishes naturally under N. Prompts submitted during
  selection remain in participant memory and are sent once after N+1 opens.
- Selection emits no signal, EOF, close, cancel, interrupt, stop, process
  replacement, TUI reconnect, app-server replacement, thread resume, or event
  replay on success, failure, timeout, or recovery.
- Before provider commit, failure reopens epoch N. After any observer sees the
  target, recovery moves forward only. Provider credentials never roll back.
- Only saved accounts are focusable. Ambient or unmanaged provider state is
  nonfocusable status and never changes panel counts.
- Every focused Enter path returns a typed queued intent or a visible typed
  refusal. A visible focused row may never silently return `None`.
- Prompt-toolkit alone owns interactive first paint, resize, scrolling,
  invalidation, and restoration. Bootstrap and entrypoint code never paint or
  issue relative cursor movement.
- Layout decisions use both columns and rows. The masthead becomes compact
  when required, the saved-account body scrolls to the focused stable ID, and
  status plus key help stay fixed and visible.
- Claude refreshable selection reuses the official native login transaction
  and native identity, generation, and propagation proof. Ordinary foreground
  Claude presence is not Remote Control proof.
- The Claude protected lease plane and structured-host prototype may be built
  only behind a disabled capability. Setup/mixed selection remains visibly
  unavailable until exact-build auth, complete exposed-host parity, genuine-
  turn identity, security, forward recovery, and written Anthropic gates pass.
- A private Claude update acknowledgement is an install receipt only. It is
  never provider identity, READY, or next-turn adoption proof.
- Codex uses one neutral, token-free interactive `CODEX_HOME`, one resident
  app server, current external auth, and the direct ChatGPT Codex Responses
  provider with model WebSockets disabled. The TUI control connection stays
  open.
- The Linux/WSL supervisor unit uses `KillMode=process`. Sidekick reaps its
  bounded workers, while the official detached Codex daemon survives a
  supervisor replacement. `KillMode=mixed` is prohibited because live cutover
  proved that it kills the daemon and connected conversation.
- Preserve all-account maintenance independently of selection. Selected and
  unselected refreshable accounts remain fresh and reportable; setup tokens
  remain usage-capable but are never called refreshable.
- Reuse prompt-toolkit, websockets, the existing strict JSON-RPC codec,
  filesystem transactions, locks, paths, clocks, provider services, and
  bounded worker machinery. Add no dependency unless a measured, maintained
  library replaces more code and maintenance than it introduces.
- Do not add a Claude Agent SDK merely because it is maintained. It must remove
  existing owned machinery while preserving the private auth seam and cannot
  coexist as a second compatibility layer.
- Use cohesive classes for participant registries, coordinators, structured
  sessions, and relays. Keep stateless parsing and validation as small typed
  functions. Apply the rule of three before extracting another abstraction.
- Use double quotes, four spaces, concise Sphinx-style docstrings, explicit
  types, and no `Any`, unjustified `cast`, blanket suppression, or deferred
  annotation import. Every new or changed code, comment, and docstring line is
  at most 79 characters.
- Do not grow a module beyond 1000 lines. Split a module near 800 lines only at
  a real cohesion boundary. In particular, do not add behavior to the current
  905-line dashboard session or 826-line Codex broker responder.
- Claude Task 8 has exactly three consolidated load-bearing journeys: native
  and mixed continuity, security and forward recovery, and exact-build host
  qualification. Extend existing tests/fakes. Add no matrices, helper tests,
  snapshots, duplicate provider journeys, new fake modules, process-helper
  tests, or coverage-padding cases.
- Automated tests use synthetic identities, tokens, homes, executables, and
  sockets. They never require real credentials, public network access, or
  provider-login mutation.
- Provider-live qualification is a controlled release gate with disposable
  accounts and separate explicit authority. It is never an ordinary test or a
  side effect of implementing a task.

---

- **Status:** Implementation active; Claude setup/mixed release-disabled
- **Date:** 2026-08-01; Claude evidence amendment 2026-08-03
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Planning baseline:** `70b70341c8cfc53127f40a2f4c14de6f21beb2f0`
- **Implementation branch:** `feat/hardened-global-account-selection`
- **Required platforms:** Linux, WSL, macOS Arm64, macOS Intel
- **Initial shell integration:** Bash, Zsh, Fish
- **Claude qualified behavior baseline:** 2.1.220 structured stream
- **Codex qualified behavior baseline:** 0.146.0 app-server protocol

## 1. Execution Preflight and Reporting Continuity

Implementation starts only after this plan is on `origin/develop`.

- [ ] Invoke `superpowers:using-git-worktrees` and create
  `/home/sabossedgh/dev/.worktrees/`
  `sidekick-usages-hardened-global-selection` from the then-current
  `origin/develop` with branch
  `feat/hardened-global-account-selection`.

```bash
git fetch origin
git worktree add \
  /home/sabossedgh/dev/.worktrees/sidekick-usages-hardened-global-selection \
  -b feat/hardened-global-account-selection origin/develop
```

- [ ] In the worktree, prove the approved design and this plan are present,
  record the exact base in ignored scratch state, and require a clean tree.

```bash
git status --short --branch
git rev-parse HEAD > \
  .agents/tmp/hardened-global-account-selection-base.txt
test -f docs/superpowers/specs/\
2026-08-01-hardened-global-account-selection-design.md
test -f docs/superpowers/plans/\
2026-08-01-hardened-global-account-selection.md
```

- [ ] Record, but do not replace, the installed reporting executable.

```bash
command -v sidekick-usages
sidekick-usages --version
sha256sum "$(readlink -f "$(command -v sidekick-usages)")"
```

Expected: `/home/sabossedgh/.local/bin/sidekick-usages`, version `0.7.0`, and
the SHA-256 in Global Constraints.

- [ ] Prove the current WSL compatibility reporting path before feature work.

```bash
test -z "${WSL_DISTRO_NAME-}"
env WSL_DISTRO_NAME=Ubuntu \
  sidekick-usages --no-interactive check >/dev/null
```

Expected: exit zero. Keep this process-local invocation available throughout
feature work. Do not persist the workaround in a shell profile or wrapper; the
source/platform repair in Task 12 and final wheel must remove its necessity.

- [ ] Run the source baseline gates without invoking the working-tree CLI
  against live paths.

```bash
uv sync --all-groups
uv run pytest tests/dashboard tests/daemon \
  tests/credentials/claude tests/credentials/codex
uv run ruff check src/ tests/ packaging/
uv run ty check src/ tests/ packaging/
uv run python packaging/check_architecture.py
```

- [ ] Keep the installed command available for the user's normal reporting.
  No task below changes the uv tool installation, its launcher, live Sidekick
  application paths, Claude native state, or any private Codex home. A feature
  task that cannot meet this rule stops before mutation.

## 2. Target File Map

The map is normative for implementation. A task may remove a listed file only
when its behavior is moved to the named owner in the same commit.

### Existing owners to modify

| Owner | Responsibility after this plan |
| --- | --- |
| `cli/runtime/bootstrap.py` | Route interactive versus one-shot execution; never paint |
| `cli/dashboard/application.py` | One prompt-toolkit terminal lifecycle and height-aware containers |
| `cli/dashboard/session.py` | Input/action session state after lookup orchestration is extracted |
| `usage/dashboard/models.py` | Saved rows, provider status, stable-ID focus, typed operation status |
| `usage/dashboard/service.py` | Cached-first saved-only projection and independent usage join |
| `usage/presentation/dashboard/render/` | Semantic masthead, body, status, and key fragments |
| `core/selection/` | Infrastructure-free epochs, durable selection models, and transition policy |
| `persistence/supervisor/selection.py` | Finalized provider selection compare-and-swap |
| `daemon/control/` | Same-user strict participant and operator protocol |
| `daemon/runtime/supervisor.py` | Resident registry, coordinator, recovery, and bounded connections |
| `daemon/worker/` | Existing bounded provider-work and exchange lane |
| `doctor/runtime/` | Safe runtime, participant, and capability diagnostics |
| `providers/claude/activation/` | Native guard and official native selection adapter |
| `providers/codex/app_server/` | Exact schema, effective-config, and external-auth capability proof |
| `providers/codex/broker/` | Existing resident app-server and external-auth projection |
| `usage/service.py` | Selection-independent, ordered all-account collection |
| `paths.py` | Every Sidekick application, session, journal, and runtime path |

### Cohesive files to create

| File | Single responsibility |
| --- | --- |
| `core/identifiers.py` | Shared canonical UUID base already needed by account and participant IDs |
| `persistence/schema/selection_operation.py` | Strict versioned journal codec |
| `daemon/selection/__init__.py` | Thin package marker only |
| `daemon/selection/models.py` | Ephemeral participant, connection, turn, gate, and snapshot models |
| `daemon/selection/ports.py` | Provider adapter and participant event boundaries |
| `daemon/selection/registry.py` | Process-proven live participants and turn leases |
| `daemon/selection/coordinator.py` | Serialized epoch transaction and admission barrier |
| `daemon/selection/recovery.py` | Provider-first forward recovery from active journals |
| `cli/dashboard/lookup.py` | Extracted cached lookup overlay and worker observation lifecycle |
| `cli/session/__init__.py` | Thin package marker only |
| `cli/session/models.py` | Provider launcher, shell, and exit-result models |
| `cli/session/launcher.py` | Exact executable resolution and argument/environment validation |
| `cli/session/shell.py` | Reversible Bash, Zsh, and Fish integration service |
| `cli/session/claude.py` | User-facing Claude structured terminal host |
| `cli/session/codex.py` | Stock Codex TUI process owner and relay lifecycle |
| `cli/commands/session.py` | Public `session` command group only |
| `cli/contexts/session.py` | Typed lazy composition for session commands |
| `persistence/shell.py` | Qualified marked shell-startup edits and generated-file writes |
| `providers/claude/structured/__init__.py` | Thin package marker only |
| `providers/claude/structured/models.py` | Strict stream events, capability, and readiness models |
| `providers/claude/structured/codec.py` | Bounded JSON-lines input/output and correlated control response |
| `providers/claude/structured/process.py` | Official structured-engine subprocess and pipe lifecycle |
| `providers/claude/structured/session.py` | Turn state and install receipts |
| `credentials/claude/authority/access_lease.py` | Target lease |
| `providers/claude/structured/data_plane.py` | Protected delivery |
| `providers/codex/session/__init__.py` | Thin package marker only |
| `providers/codex/session/models.py` | Exact session capability and relay event models |
| `providers/codex/session/config.py` | Protected HTTP-only provider overlay and effective proof |
| `providers/codex/session/relay.py` | TUI/app-server control relay and new-turn gate |

Do not create parallel `*_types.py`, `helpers.py`, compatibility adapters, a
second JSON-RPC stack, a model proxy, or another credential repository.
Add participant/epoch types to existing `core/selection/types.py`, operation
models to existing `core/selection/models.py`, operation documents to existing
`persistence/models/selection.py`, and the journal store to existing
`persistence/supervisor/selection.py`. These owners remain well below their
cohesion limits; only the already-681-line selection schema receives a separate
operation-schema file.

### Test file ceiling

Create only these two test modules:

- `tests/daemon/test_selection.py` for the provider-neutral schedule and crash
  recovery proof;
- `tests/cli/test_sessions.py` for launcher, shell, and same-process provider
  journey proof.

Every other change extends the nearest existing owner test. This is a ceiling,
not a target. Each numbered task specifies its one or two load-bearing proofs.

## 3. Cross-Task Interface Contract

These exact names prevent adjacent tasks from inventing parallel concepts.

```python
@dataclass(frozen=True, slots=True, order=True)
class SelectionEpoch:
    value: int

    def next(self) -> Self: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class FinalizedSelection:
    provider_id: ProviderId
    account_id: SidekickAccountId
    epoch: SelectionEpoch
    generation: AuthorityGeneration
    finalized_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class PreparedSelection:
    operation_id: OperationId
    provider_id: ProviderId
    target_account_id: SidekickAccountId
    target_generation: AuthorityGeneration
    baseline_epoch: SelectionEpoch
    pending_epoch: SelectionEpoch


@dataclass(frozen=True, slots=True, kw_only=True)
class AuthorityReadyProof:
    provider_id: ProviderId
    account_id: SidekickAccountId
    generation: AuthorityGeneration
    epoch: SelectionEpoch
    safe_code: SelectionCode


class SelectionRecoveryRelation(StrEnum):
    BASELINE_PROVEN = "baseline_proven"
    TARGET_PROVEN = "target_proven"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectionRecoveryDecision:
    relation: SelectionRecoveryRelation
    target_generation: AuthorityGeneration | None
    safe_code: SelectionCode


class SelectionAuthorityAdapter(Protocol):
    def prevalidate(
        self,
        operation: OpenSelectionOperation,
        baseline: FinalizedSelection | None,
    ) -> PreparedSelection: ...

    def commit(
        self,
        prepared: PreparedSelection,
    ) -> AuthorityReadyProof: ...

    def readback(
        self,
        prepared: PreparedSelection,
    ) -> SelectionRecoveryDecision: ...
```

`PreparedSelection.operation_id` must equal `operation.operation_id`; the
adapter validates but never invents coordinator identity or epoch values.
Provider calls execute through the existing bounded worker/exchange lane.
They do not run on the supervisor selector or create another executor.

Provider adapter results and subclasses remain secret-free. A protected lease
travels only through the provider-owned worker exchange and capability channel.
Its context-managed mutable owner has a redacted `repr`, bounded lifetime, and
immediate cleanup; it never enters result serialization.

Claude computes `SelectionRecoveryDecision` from target authority mode, safe
worker proof, native readback, and exact secret-free participant binding
queries. Native baseline cannot prove rollback for a setup target. Any target
participant binding returns `TARGET_PROVEN`; conflicting or incomplete
evidence returns `UNRESOLVED` and leaves admission closed.

Participant commands use the same strict supervisor protocol:

```python
class ParticipantControlPort(Protocol):
    def register(
        self,
        manifest: ParticipantManifest,
    ) -> ParticipantRegistration: ...

    def begin_turn(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        turn_id: TurnId,
    ) -> TurnAdmission: ...

    def end_turn(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        turn_id: TurnId,
    ) -> None: ...

    def ready(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        proof: ParticipantReadyProof,
    ) -> None: ...

    def adopt(
        self,
        participant_id: ParticipantId,
        connection_generation: int,
        proof: ParticipantAdoptionProof,
    ) -> None: ...
```

The server retains authenticated peer evidence in one immutable dispatch
context:

```python
@dataclass(frozen=True, slots=True)
class VerifiedControlRequest:
    request: ControlRequest
    peer: PeerIdentity


class ControlDispatcher(Protocol):
    def dispatch(
        self,
        context: VerifiedControlRequest,
    ) -> Iterator[ControlEvent]: ...
```

`PeerIdentity` always proves the effective user. It carries optional
kernel-proven process identity; participant registration requires it, while
ordinary operator requests need only the same-user proof.

Every public result and refusal uses this one closed vocabulary; provider
adapters redact and map provider failures before returning it:

```python
class SelectionCode(StrEnum):
    ALREADY_SELECTED = "already_selected"
    SELECTION_SUCCEEDED = "selection_succeeded"
    SELECTION_READY_ADOPTION_PENDING = (
        "selection_ready_adoption_pending"
    )
    TARGET_REFRESH_REQUIRED = "target_refresh_required"
    TARGET_EXPIRED = "target_expired"
    TARGET_REJECTED = "target_rejected"
    TARGET_MALFORMED = "target_malformed"
    TARGET_UNREADABLE = "target_unreadable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    UNSUPPORTED_PROVIDER_VERSION = "unsupported_provider_version"
    UNSUPPORTED_SESSION_CAPABILITY = (
        "unsupported_session_capability"
    )
    SESSION_CONFIGURATION_REQUIRED = (
        "session_configuration_required"
    )
    UNCOORDINATED_AUTH_MUTATION = "uncoordinated_auth_mutation"
    REMOTE_CONTROL_STATE_INCOMPATIBLE = (
        "remote_control_state_incompatible"
    )
    PARTICIPANT_UNREACHABLE = "participant_unreachable"
    PARTICIPANT_CONFIRMED_DEAD = "participant_confirmed_dead"
    PARTICIPANT_LOST_AFTER_COMMIT = "participant_lost_after_commit"
    REALTIME_SESSION_ACTIVE = "realtime_session_active"
    ACTIVE_OPERATION_TIMEOUT = "active_operation_timeout"
    AUTHORITY_PROOF_FAILED = "authority_proof_failed"
    SELECTION_ROLLED_BACK = "selection_rolled_back"
    SELECTION_RECOVERY_REQUIRED = "selection_recovery_required"
```

The host or relay, never the supervisor, owns queued prompt bodies. The
coordinator knows only counts, IDs, epochs, and safe state.

## 4. Implementation Tasks

### Task 1: Remove Pseudo-Accounts and Repair Stable Focus

**Files:**

- Modify: `src/sidekick_usages/usage/dashboard/models.py`
- Modify: `src/sidekick_usages/usage/dashboard/service.py`
- Modify: `src/sidekick_usages/usage/dashboard/focus.py`
- Modify: `src/sidekick_usages/cli/dashboard/controller.py`
- Modify: `src/sidekick_usages/cli/dashboard/actions.py`
- Modify: `src/sidekick_usages/usage/presentation/dashboard/selection.py`
- Modify: `src/sidekick_usages/usage/presentation/dashboard/render/narrow.py`
- Modify: `src/sidekick_usages/usage/presentation/dashboard/render/wide.py`
- Modify: `src/sidekick_usages/usage/presentation/dashboard/render/text.py`
- Test: `tests/usage/test_dashboard_render.py`
- Test: `tests/dashboard/test_state.py`
- Test: `tests/dashboard/test_routing.py`

**Interfaces:**

- Consumes: existing persisted saved-account order and passive provider
  runtime observations.
- Produces: `DashboardProvider.rows: tuple[DashboardAccount, ...]`,
  `DashboardCursor(focused_provider, account_id)`, and nonfocusable
  `DashboardProviderStatus`.

- [ ] **Step 1: Collapse the current external-row tests into one failing
  saved-only contract.**

```python
def test_dashboard_projects_and_navigates_only_saved_accounts() -> None:
    snapshot = dashboard_with_external_runtime()

    rows = tuple(
        row
        for provider in snapshot.providers
        for row in provider.rows
    )
    assert [row.account_id for row in rows] == SAVED_ACCOUNT_IDS
    assert [len(provider.rows) for provider in snapshot.providers] == [4, 2]
    assert initial_dashboard_cursor(snapshot).account_id == CLAUDE_ACCOUNT_ID
    assert controller(snapshot).enter().kind is DashboardIntentKind.SELECT
```

The fixture uses synthetic IDs. Delete assertions that expect an external row,
external cursor, or no-op Enter. In the same fixture, give one saved Codex row
a newer related generation and require it to remain that saved row with
reconciliation status. Preserve the current empty-provider/product navigation
proof rather than adding another test.

- [ ] **Step 2: Run the focused proof and verify the current model fails.**

```bash
uv run pytest \
  tests/usage/test_dashboard_render.py \
  tests/dashboard/test_state.py \
  tests/dashboard/test_routing.py -q
```

Expected: failure because `DashboardExternalRow` is still projected or focused.

- [ ] **Step 3: Make rows saved-only and focus stable by account ID.**

Use these model shapes:

```python
type DashboardRow = DashboardAccount


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardProviderStatus:
    runtime_state: ProviderRuntimeState | None
    observed_at: datetime | None
    unmanaged_sessions: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class DashboardCursor:
    focused_provider: ProviderId | None
    account_id: SidekickAccountId | None
```

Remove `DashboardExternalRow`, `DashboardActionState.EXTERNAL_ACTIVE`, and the
cursor's `external` flag. `DashboardProvider.rows` contains saved rows only.
When a provider has saved rows, `provider_focus()` returns its verified active
saved ID or its first saved ID. Ambient runtime differences render below the
provider title as status and never count, focus, or select.

- [ ] **Step 4: Make every Enter path typed and visible.**

`DashboardController.activate()` returns a selection/repair intent for a saved
ID or a typed refusal with one `SelectionCode`. Remove every external-row
early return. Preserve existing setup, repair, refresh, and association intents
only where a saved row's credential capability requires them.

- [ ] **Step 5: Run the focused tests and formatting check.**

```bash
uv run pytest \
  tests/usage/test_dashboard_render.py \
  tests/dashboard/test_state.py \
  tests/dashboard/test_routing.py -q
uv run ruff format --check \
  src/sidekick_usages/usage/dashboard \
  src/sidekick_usages/cli/dashboard \
  src/sidekick_usages/usage/presentation/dashboard \
  tests/usage/test_dashboard_render.py \
  tests/dashboard/test_state.py \
  tests/dashboard/test_routing.py
```

- [ ] **Step 6: Commit the saved-only interaction contract.**

```bash
git add src/sidekick_usages/usage/dashboard \
  src/sidekick_usages/cli/dashboard \
  src/sidekick_usages/usage/presentation/dashboard \
  tests/usage/test_dashboard_render.py \
  tests/dashboard/test_state.py tests/dashboard/test_routing.py
git commit -m "fix(dashboard): remove external pseudo-accounts"
```

### Task 2: Give Prompt-Toolkit Sole, Height-Aware Terminal Ownership

**Files:**

- Modify: `src/sidekick_usages/cli/runtime/bootstrap.py`
- Delete: `src/sidekick_usages/cli/dashboard/launch.py`
- Modify: `src/sidekick_usages/cli/dashboard/application.py`
- Modify: `src/sidekick_usages/cli/dashboard/terminal.py`
- Create: `src/sidekick_usages/cli/dashboard/lookup.py`
- Modify: `src/sidekick_usages/cli/dashboard/session.py`
- Modify: `src/sidekick_usages/branding/content.py`
- Modify: `src/sidekick_usages/usage/presentation/dashboard/render/models.py`
- Modify: `src/sidekick_usages/usage/presentation/dashboard/render/frame.py`
- Modify: `src/sidekick_usages/usage/presentation/dashboard/render/text.py`
- Test: `tests/dashboard/test_pty.py`
- Test: `tests/usage/test_dashboard_render.py`

**Interfaces:**

- Consumes: saved-only `DashboardSnapshot` and `DashboardCursor` from Task 1.
- Produces: `TerminalDimensions`, `DashboardRenderLayout`, and one body cursor
  line used by prompt-toolkit scrolling.

- [ ] **Step 1: Replace the duplicate-logo regression with one PTY matrix.**

```python
TERMINAL_SIZES = (
    (52, 24),
    (79, 40),
    (80, 48),
    (100, 49),
    (120, 60),
)


@pytest.mark.parametrize(("columns", "rows"), TERMINAL_SIZES)
def test_dashboard_has_one_masthead_and_a_visible_key_footer(
    columns: int,
    rows: int,
) -> None:
    capture = run_dashboard_screen(columns=columns, rows=rows)

    assert capture.scrollback.count("sidekick usages") == 1
    assert KEY_FOOTER_TEXT in capture.visible
    assert "External Claude Code login" not in capture.visible
    assert "External Codex CLI login" not in capture.visible
```

Keep one existing journey and add one resize assertion to it: focus a saved
ID, resize from `(100, 49)` to `(52, 24)`, and require the same ID plus visible
footer. Preserve the existing normal, interrupt, and failure restoration
proofs. Add one semantic-layout assertion below the supported height that
requires the typed `TERMINAL_TOO_SHORT` status and visible keys. Do not create
a width-by-height Cartesian suite or another PTY journey.

- [ ] **Step 2: Run the PTY proof and preserve its failing transcript.**

```bash
uv run pytest tests/dashboard/test_pty.py -q
```

Expected: the short terminal sees duplicate masthead output or a clipped key
footer before terminal ownership changes.

- [ ] **Step 3: Remove cached interactive paint from bootstrap.**

`bootstrap.main()` still selects interactive versus one-shot mode, but the
interactive path immediately executes the dashboard process image. Remove
`present_cached_dashboard()`, `present_dashboard_frame()`, relative cursor-up,
and failed-replace cursor-down behavior. The dashboard entrypoint loads cached
state before prompt-toolkit's first render.

- [ ] **Step 4: Extract the current lookup lifecycle before changing layout.**

Move `_run_lookup` through `_load_lookup_snapshot` and their immutable overlay
state from the 905-line session into this cohesive owner:

```python
class DashboardLookupCoordinator:
    def start(self) -> None: ...

    def close(self) -> None: ...

    def apply(
        self,
        snapshot: DashboardSnapshot,
    ) -> DashboardSnapshot: ...
```

`InteractiveDashboardSession` retains navigation, action submission, and
footer state. The extraction must be behavior-preserving and leave both files
below the repository's cohesion threshold where practical.

- [ ] **Step 5: Render semantic layout fragments for prompt-toolkit.**

```python
@dataclass(frozen=True, slots=True)
class TerminalDimensions:
    columns: int
    rows: int


@dataclass(frozen=True, slots=True)
class DashboardRenderLayout:
    masthead: str
    body: str
    status: str
    keys: str
    focused_body_line: int | None
```

`render_dashboard_layout()` chooses full or minimal masthead from height,
renders the account panels into the body, and keeps status and keys separate.
Retain `render_dashboard()` for one-shot noninteractive output by joining the
same semantic fragments. Extend `brand_layout(width, *, compact=False)`; do not
create another robot or product-copy source. Below the supported height, keep
the body scrollable and emit `TERMINAL_TOO_SHORT` in the fixed status area;
never hide the keys.

- [ ] **Step 6: Build one prompt-toolkit `HSplit` lifecycle.**

Use fixed preferred-height masthead, status, and key `Window` instances around
one filling body `Window`. Supply its hidden cursor through
`FormattedTextControl(get_cursor_position=...)` so prompt-toolkit scrolls the
focused saved row into view. Read `get_app().output.get_size()` during render;
do not infer height from zoom, width, or frame line count.

Keep one-shot/non-TTY rendering on `render_dashboard()` and prove in the
existing render test that it is finite and contains no cursor or alternate-
screen escapes.

- [ ] **Step 7: Run the PTY matrix and focused dashboard tests.**

```bash
uv run pytest tests/dashboard/test_pty.py \
  tests/usage/test_dashboard_render.py tests/dashboard/test_state.py -q
uv run ruff check src/sidekick_usages/cli/runtime/bootstrap.py \
  src/sidekick_usages/cli/dashboard \
  src/sidekick_usages/usage/presentation/dashboard \
  src/sidekick_usages/branding tests/dashboard tests/usage
```

- [ ] **Step 8: Commit the single-painter responsive dashboard.**

```bash
git add src/sidekick_usages/cli/runtime/bootstrap.py \
  src/sidekick_usages/cli/dashboard \
  src/sidekick_usages/usage/presentation/dashboard \
  src/sidekick_usages/branding/content.py \
  tests/dashboard/test_pty.py tests/usage/test_dashboard_render.py
git commit -m "fix(dashboard): make interactive layout height aware"
```

### Task 3: Add Non-Secret Epoch and Recovery Persistence

**Files:**

- Create: `src/sidekick_usages/core/identifiers.py`
- Modify: `src/sidekick_usages/core/accounts/types.py`
- Modify: `src/sidekick_usages/core/selection/models.py`
- Modify: `src/sidekick_usages/core/selection/types.py`
- Modify: `src/sidekick_usages/core/selection/policy.py`
- Create: `src/sidekick_usages/persistence/schema/selection_operation.py`
- Modify: `src/sidekick_usages/persistence/models/selection.py`
- Modify: `src/sidekick_usages/persistence/schema/selection.py`
- Modify: `src/sidekick_usages/persistence/supervisor/selection.py`
- Modify: `src/sidekick_usages/paths.py`
- Modify: `tests/support/persistence.py`
- Create: `tests/daemon/test_selection.py`
- Test: `tests/daemon/test_state.py`
- Test: `tests/test_paths.py`

**Interfaces:**

- Consumes: existing canonical UUID, `ManagedStateFilesystem`, atomic write,
  lock, strict JSON, and selected-state compare-and-swap owners.
- Produces: the cross-task models in Section 3 and
  `SelectionOperationStore`.

- [ ] **Step 1: Write one failing transition-and-round-trip schedule.**

```python
def test_selection_journal_is_forward_only_and_secret_free(
    application_paths: ApplicationPaths,
) -> None:
    operation = open_selection_operation()
    store = SelectionOperationStore(application_paths.selection_journals)

    store.begin(operation)
    store.compare_and_swap(operation, waiting_old_turns(operation))
    store.compare_and_swap(
        waiting_old_turns(operation),
        awaiting_ready(operation),
    )
    store.complete(degraded_target_result(operation))

    assert store.load(PROVIDER_ID).active is None
    assert store.load(PROVIDER_ID).history[-1].lost_count == 1
    assert SECRET_CANARY not in persisted_selection_bytes(application_paths)
```

Extend this single schedule with crash injection at each durable write and
assert the design's recovery classification. Do not create one test function
per phase.

- [ ] **Step 2: Run the test and verify missing epoch/journal types fail.**

```bash
uv run pytest tests/daemon/test_selection.py tests/daemon/test_state.py -q
```

- [ ] **Step 3: Reuse one public canonical UUID base.**

Move only the existing private `_CanonicalUuid` implementation into
`core/identifiers.py` as `CanonicalUuid`. Keep current account ID public import
paths stable, and add `ParticipantId` plus `TurnId` subclasses in
`core/selection/types.py`. Do not duplicate UUID parsing.

- [ ] **Step 4: Add exact epoch and selection operation models.**

`SelectionEpoch` accepts integers from zero through `2**63 - 1`; `next()`
fails closed at the upper bound. Add closed enums for:

```python
class SelectionPhase(StrEnum):
    PREVALIDATING = "prevalidating"
    PREPARING = "preparing"
    WAITING_OLD_TURNS = "waiting_old_turns"
    COMMITTING = "committing"
    AWAITING_READY = "awaiting_ready"
    RECOVERING = "recovering"


class SelectionOutcome(StrEnum):
    READY = "ready"
    FAILED_OLD_EPOCH = "failed_old_epoch"
    PARTICIPANT_LOST_AFTER_COMMIT = "participant_lost_after_commit"
    RECOVERY_REQUIRED = "recovery_required"
```

Use immutable `OpenSelectionOperation` and `SelectionResult` plus the shared
`SelectionCode`; do not return raw safe-code strings. Open operations may
contain bounded sorted participant IDs; closed results retain only required,
ready, adopted, and lost counts.

- [ ] **Step 5: Upgrade selected state without preserving pseudo-accounts.**

The selected-state schema advances from version 2 to version 3 and persists
`FinalizedSelection`. Version-2 `SAVED_ACTIVE` records migrate to epoch zero.
Version-2 `EXTERNAL_ACTIVE`, logged-out, unreadable, and unsupported records do
not become selections. Ambient auth is re-observed separately. Preserve every
saved account document, order, authority reference, and private home. The
forward-only, idempotent migration stores one validated pre-migration snapshot
under existing Sidekick recovery policy and never reads or rewrites authority
bytes merely to advance the selection schema.

- [ ] **Step 6: Add a per-provider journal store.**

```python
class SelectionOperationStore:
    def begin(
        self,
        operation: OpenSelectionOperation,
    ) -> OpenSelectionOperation: ...

    def compare_and_swap(
        self,
        expected: OpenSelectionOperation,
        replacement: OpenSelectionOperation,
    ) -> OpenSelectionOperation: ...

    def complete(
        self,
        result: SelectionResult,
    ) -> SelectionResult: ...

    def load(
        self,
        provider_id: ProviderId,
    ) -> SelectionOperationDocument: ...
```

Bound history to 32 results, reuse the provider authority lock ordering, and
use owner-only atomic state files. Add these exact `ApplicationPaths` fields
and update every explicit test constructor once in this task:

- `selection_journals` below the Sidekick data root;
- `codex_session_home` below a non-secret Sidekick session-data root;
- `shell_integration` for the generated POSIX source file; and
- `participant_sockets` below the owner-only runtime directory.

Later tasks consume these qualified paths; they do not add another path field
or discover Sidekick paths inside provider adapters.

- [ ] **Step 7: Run persistence, architecture, and type gates.**

```bash
uv run pytest tests/daemon/test_selection.py tests/daemon/test_state.py \
  tests/persistence tests/test_paths.py -q
uv run ty check src/sidekick_usages/core \
  src/sidekick_usages/persistence tests/daemon tests/persistence
uv run python packaging/check_architecture.py
```

- [ ] **Step 8: Commit the epoch authority.**

```bash
git add src/sidekick_usages/core src/sidekick_usages/persistence \
  src/sidekick_usages/paths.py tests/support/persistence.py \
  tests/daemon/test_selection.py tests/daemon/test_state.py \
  tests/test_paths.py
git commit -m "feat(selection): persist provider epochs and recovery"
```

### Task 4: Implement Participant Registry and Turn Admission

**Files:**

- Modify: `src/sidekick_usages/platform/models.py`
- Modify: `src/sidekick_usages/platform/peer.py`
- Create: `src/sidekick_usages/daemon/selection/__init__.py`
- Create: `src/sidekick_usages/daemon/selection/models.py`
- Create: `src/sidekick_usages/daemon/selection/ports.py`
- Create: `src/sidekick_usages/daemon/selection/registry.py`
- Create: `src/sidekick_usages/daemon/selection/coordinator.py`
- Create: `src/sidekick_usages/daemon/selection/recovery.py`
- Modify: `src/sidekick_usages/daemon/types/protocol.py`
- Modify: `src/sidekick_usages/daemon/types/ports.py`
- Modify: `src/sidekick_usages/daemon/models/control.py`
- Modify: `src/sidekick_usages/daemon/models/protocol.py`
- Modify: `src/sidekick_usages/daemon/control/protocol.py`
- Modify: `src/sidekick_usages/daemon/control/client.py`
- Modify: `src/sidekick_usages/daemon/control/dispatch.py`
- Modify: `src/sidekick_usages/daemon/control/server.py`
- Modify: `src/sidekick_usages/daemon/worker/pool.py`
- Modify: `src/sidekick_usages/daemon/worker/runtime.py`
- Modify: `src/sidekick_usages/daemon/runtime/supervisor.py`
- Test: `tests/daemon/test_selection.py`
- Test: `tests/daemon/test_control.py`

**Interfaces:**

- Consumes: Task 3 epochs, operation journal, and adapter protocol.
- Produces: `ParticipantRegistry`, `SelectionCoordinator`, strict participant
  control requests, readiness snapshots, and first-real-turn adoption.

- [ ] **Step 1: Extend the Task 3 schedule with real concurrency.**

Use one state-machine test with three synthetic participants. Hold a turn on
participant 1, request B, submit a second begin on participant 2, finish the A
turn, acknowledge B readiness, and require the queued begin to open once under
B. In the same schedule, disconnect participant 3 after commit and require
`PARTICIPANT_LOST_AFTER_COMMIT`, not success or rollback.

The model schedule also retains one retry inside participant 1's N lease,
registers one late participant behind N+1, and asserts that the other
provider's epoch never changes. Parameterize only the disconnect/crash phase
to cover confirmed-dead-before-commit, live-unreachable, and
dead-after-commit recovery; do not duplicate the journey.

```python
assert first_turn.epoch == EPOCH_N
assert queued_turn.state is TurnAdmissionState.QUEUED
assert final.outcome is SelectionOutcome.PARTICIPANT_LOST_AFTER_COMMIT
assert released_turn.epoch == EPOCH_N_PLUS_ONE
assert adapter.signals == []
```

The fake adapter records forbidden process actions in `signals`.

- [ ] **Step 2: Run the coordinator proof and verify it fails.**

```bash
uv run pytest tests/daemon/test_selection.py tests/daemon/test_control.py -q
```

- [ ] **Step 3: Prove peer process identity, not only user identity.**

On Linux/WSL retain PID from `SO_PEERCRED` and read `/proc/<pid>/stat` start
ticks with bounded parsing. On macOS read `LOCAL_PEERPID` when available and
use `proc_pidinfo` start time. If process ID or start identity cannot be
proven,
participant registration fails closed; ordinary operator requests still use
same-user proof. A reconnect requires the same participant ID and process-start
identity with a strictly greater connection generation. `ControlConnection`
retains `PeerVerifier.verify()` and passes `VerifiedControlRequest` through
`ControlDispatcher`; no client-supplied PID can replace the kernel evidence.

- [ ] **Step 4: Implement an in-memory registry with no prompt storage.**

```python
class ParticipantRegistry:
    def register(
        self,
        manifest: ParticipantManifest,
        peer: ProcessIdentity,
    ) -> ParticipantRegistration: ...

    def begin_turn(
        self,
        request: TurnBeginRequest,
    ) -> TurnAdmission: ...

    def end_turn(self, request: TurnEndRequest) -> None: ...

    def close_admission(
        self,
        provider_id: ProviderId,
        pending_epoch: SelectionEpoch,
    ) -> ParticipantSnapshot: ...

    def open_admission(
        self,
        provider_id: ProviderId,
        epoch: SelectionEpoch,
    ) -> tuple[ParticipantId, ...]: ...
```

Bound participants per provider, live connections, turn leases, and pending
begin metadata. Set `MAX_PARTICIPANTS_PER_PROVIDER` to 16. A participant that
registers while selection is open joins the finalized epoch; one that registers
during `PREPARE` or later joins behind the pending gate and becomes required
for target readiness. The registry stores no user text or raw provider frame.

- [ ] **Step 5: Implement the exact selection state machine.**

`SelectionCoordinator.select()` serializes by provider and performs:

```text
PREVALIDATE -> PREPARE -> WAIT_OLD_TURNS -> COMMIT_AUTHORITY
-> READY_ACK -> FINALIZE_READY -> OPEN_ADMISSION
```

`NEXT_TURN_PROOF` is asynchronous participant state. A duplicate same-target
request observes the active operation; a different target returns a typed
conflict. Every phase uses an injected monotonic deadline and condition/event
wakeup, never sleep polling. Reuse the worker's 120-second provider deadline;
use 120 seconds for old-turn drain and 30 seconds for readiness. A pre-commit
timeout reopens N. A post-commit timeout keeps admission gated and returns
`SELECTION_RECOVERY_REQUIRED`; no timeout signals or terminates work.

Write and execute the phases in this exact order:

1. persist the `PREVALIDATING` operation with baseline and target IDs;
2. submit adapter prevalidation through the existing durable scheduler and
   bounded `WorkerPool`/worker-exchange path;
3. persist `PREPARING`, close admission, publish prepare, then persist
   `WAITING_OLD_TURNS`;
4. after every old lease drains, persist `COMMITTING` before submitting the
   provider mutation through the same bounded path; seal the final required
   membership through protected distribution and provider-proof binding;
5. validate the provider-owned recovery decision, persist `AWAITING_READY`,
   unseal registration, and collect every required live participant
   acknowledgement;
6. compare-and-swap `FinalizedSelection` to N+1, close the journal with the
   ready or degraded result, and only then open admission; and
7. keep later adoption receipts ephemeral.

The selector thread only accepts sockets, wakes the scheduler, and publishes
state. Existing bounded connection threads may wait on coordinator conditions,
while provider work runs only in the current worker/exchange lane. Worker
results contain safe metadata; protected leases use the existing in-memory
exchange and never enter the operation queue, journal, or event hub.

The active operation compare-and-swap, not a filesystem lock held across a
turn, serializes one provider. Claude and Codex may progress concurrently only
when the current lock graph proves their authority and shared-runtime resources
are disjoint. Provider workers acquire locks in this order: provider selection,
deterministically sorted account authorities, provider-native/shared runtime,
then persistence commit. No credential lock is held while waiting for a turn,
participant, or user input.

Have the same state-machine fake record lock acquisition so one assertion
protects the order above and proves a timeout never deletes a live owner.

- [ ] **Step 6: Extend the strict control protocol.**

Bump `PROTOCOL_VERSION` once and add closed request/event payloads for
registration, participant subscription, turn begin/end, readiness, adoption,
selection, and selection status. Retain frame-size, request-count, package
version, same-user, and strict-field checks. Long-lived participant
subscriptions receive prepare/open/status notices; acknowledgements use
ordinary bounded requests. Raise the connection ceiling to a documented bound
of 68: two connections for each of 16 participants per provider, plus four
operator connections. Reject excess connections without dropping registered
participants or changing admission; do not add an unbounded thread-per-client
policy.

- [ ] **Step 7: Add recovery before supervisor readiness.**

For every active journal, consume the adapter's safe composite recovery
decision, reconcile reachable participants, and follow the normative design's
Section 8.5 decision table. A missing participant is unreachable until
process-start proof says dead. Native account equality is not generic rollback
proof. Ambiguity keeps admission closed and publishes
`SELECTION_RECOVERY_REQUIRED`.

- [ ] **Step 8: Run control, state, and architecture gates.**

```bash
uv run pytest tests/daemon/test_selection.py \
  tests/daemon/test_control.py tests/daemon/test_runtime.py -q
uv run ty check src/sidekick_usages/daemon \
  src/sidekick_usages/platform tests/daemon
uv run python packaging/check_architecture.py
```

- [ ] **Step 9: Commit the provider-neutral coordinator.**

```bash
git add src/sidekick_usages/daemon src/sidekick_usages/platform \
  tests/daemon
git commit -m "feat(selection): coordinate live participant turns"
```

### Task 5: Add Explicit Session Launchers and Reversible Shell Enrollment

**Files:**

- Create: `src/sidekick_usages/cli/session/__init__.py`
- Create: `src/sidekick_usages/cli/session/models.py`
- Create: `src/sidekick_usages/cli/session/launcher.py`
- Create: `src/sidekick_usages/cli/session/shell.py`
- Create: `src/sidekick_usages/cli/commands/session.py`
- Create: `src/sidekick_usages/cli/contexts/session.py`
- Create: `src/sidekick_usages/persistence/shell.py`
- Modify: `src/sidekick_usages/cli/app.py`
- Modify: `src/sidekick_usages/cli/context.py`
- Modify: `src/sidekick_usages/cli/contexts/models.py`
- Modify: `src/sidekick_usages/paths.py`
- Create: `tests/cli/test_sessions.py`
- Test: `tests/cli/test_help.py`
- Test: `tests/test_paths.py`

**Interfaces:**

- Consumes: Task 4 participant control client and process identity.
- Produces: public `sidekick-usages session` commands, exact provider launch
  specs, and idempotent shell integration.

- [ ] **Step 1: Write one launcher and shell round-trip test.**

```python
def test_session_launcher_preserves_process_and_shell_contract(
    tmp_path: Path,
) -> None:
    shell = install_bash_integration(tmp_path)
    first = shell.install(dry_run=False)
    second = shell.install(dry_run=False)
    launch = session_launcher(tmp_path).plan(
        ProviderId.CLAUDE,
        ("--model", "sonnet", "prompt with spaces"),
    )

    assert first.changed is True
    assert second.changed is False
    assert launch.provider_arguments == (
        "--model",
        "sonnet",
        "prompt with spaces",
    )
    assert shell.uninstall(dry_run=False).changed is True
    assert unrelated_shell_text(tmp_path) == ORIGINAL_SHELL_TEXT
```

Extend the same test with Fish generation and one unsafe override refusal. Do
not test every flag or quoting character separately.

- [ ] **Step 2: Run the focused CLI test and verify commands are absent.**

```bash
uv run pytest tests/cli/test_sessions.py tests/cli/test_help.py \
  tests/test_paths.py -q
```

- [ ] **Step 3: Add exact launcher validation.**

```python
class ProviderSessionLauncher:
    def plan(
        self,
        provider_id: ProviderId,
        provider_arguments: tuple[str, ...],
    ) -> SessionLaunchSpec: ...

    def run(self, spec: SessionLaunchSpec) -> int: ...
```

Resolve the official provider launcher through the existing executable
qualification owners, freeze its exact target for the launch, reject recursion
into Sidekick, preserve cwd/TTY/size/signals/argv/exit status, and reject NULs.
Provider-specific validators reject auth, endpoint, home, transport, `--bare`,
and unsafe config overrides with typed errors. They never delete or reorder an
argument silently.

- [ ] **Step 4: Implement qualified marked shell edits.**

`ShellStartupResolver` uses the injected environment and platform to resolve
one Bash or Zsh interactive startup file, the Sidekick-owned generated POSIX
script, or Fish's `conf.d/sidekick-usages.fish`. It rejects relative HOME,
relative `ZDOTDIR`/`XDG_CONFIG_HOME`, symlinks, cross-owner files, and multiple
conflicting viable startup files as typed ambiguity. An absent, uniquely
resolved canonical file may be created atomically with current-user ownership.

Bash and Zsh receive one bounded marked source line pointing at the owner-only
generated POSIX script. Fish receives one owner-only
`conf.d/sidekick-usages.fish`. The generated functions are exactly:

```sh
claude() {
    command sidekick-usages session claude -- "$@"
}
codex() {
    command sidekick-usages session codex -- "$@"
}
```

Fish uses `$argv` and `command sidekick-usages`. The persistence owner uses a
stable read, current-user ownership, non-symlink checks, bounded size, atomic
write, and compare-before-remove. A changed or ambiguous source block fails
closed and prints its exact manual removal range. Uninstall removes only an
exact marked source line and byte-matching generated file. Dry run reports
paths, preconditions, and diffs without creating parents or files.

- [ ] **Step 5: Register the public command group.**

Expose exactly:

```text
sidekick-usages session claude -- [CLAUDE_ARGUMENTS...]
sidekick-usages session codex -- [CODEX_ARGUMENTS...]
sidekick-usages session shell install [--shell ...] [--dry-run]
sidekick-usages session shell uninstall [--shell ...] [--dry-run]
sidekick-usages session shell status [--shell ...]
```

The provider commands remain capability-refused until their later host/relay
tasks are complete. They must not fall through to an unmanaged launch while
claiming integration.

- [ ] **Step 6: Run CLI, persistence, type, and architecture gates.**

```bash
uv run pytest tests/cli/test_sessions.py tests/cli/test_help.py \
  tests/test_paths.py \
  tests/persistence -q
uv run ruff check src/sidekick_usages/cli/session \
  src/sidekick_usages/cli/commands/session.py \
  src/sidekick_usages/persistence/shell.py tests/cli/test_sessions.py
uv run ty check src/sidekick_usages/cli \
  src/sidekick_usages/persistence tests/cli
uv run python packaging/check_architecture.py
```

- [ ] **Step 7: Commit explicit enrollment without installing it live.**

```bash
git add src/sidekick_usages/cli src/sidekick_usages/persistence \
  src/sidekick_usages/paths.py tests/cli/test_sessions.py \
  tests/cli/test_help.py tests/test_paths.py
git commit -m "feat(session): add explicit provider enrollment"
```

### Task 6: Correct Claude Native Selection and Remote Control Proof

**Files:**

- Modify: `src/sidekick_usages/providers/claude/activation/types.py`
- Modify: `src/sidekick_usages/providers/claude/activation/foreground.py`
- Modify: `src/sidekick_usages/providers/claude/activation/service.py`
- Modify: `src/sidekick_usages/credentials/claude/activation/service.py`
- Modify: `src/sidekick_usages/daemon/worker/claude/selection.py`
- Modify: `src/sidekick_usages/cli/commands/use.py`
- Modify: `src/sidekick_usages/cli/dashboard/actions.py`
- Test: `tests/credentials/claude/test_activation.py`
- Test: `tests/providers/claude/test_managed_boundaries.py`

**Interfaces:**

- Consumes: existing official `claude auth login --claudeai` transaction,
  protected target proof, native generation, and mtime proof.
- Produces: a `SelectionAuthorityAdapter` for refreshable Claude and a session
  capability guard that blocks only proven incompatible Remote Control.

- [ ] **Step 1: Replace foreground-presence tests with one capability test.**

```python
def test_native_switch_ignores_tty_but_blocks_proven_remote_control() -> None:
    ordinary = native_adapter(
        foreground=ClaudeForegroundState.PRESENT,
        remote_control=ClaudeRemoteControlState.INACTIVE,
    )
    incompatible = native_adapter(
        foreground=ClaudeForegroundState.PRESENT,
        remote_control=ClaudeRemoteControlState.ACTIVE_INCOMPATIBLE,
    )

    assert ordinary.prevalidate(TARGET_ID, BASELINE, EPOCH).provider_id \
        is ProviderId.CLAUDE
    with pytest.raises(ClaudeActivationGuardError) as raised:
        incompatible.prevalidate(TARGET_ID, BASELINE, EPOCH)
    assert raised.value.code == "remote_control_incompatible"
```

- [ ] **Step 2: Run the Claude activation tests and verify the old guard
  fails.**

```bash
uv run pytest tests/credentials/claude/test_activation.py \
  tests/providers/claude/test_managed_boundaries.py -q
```

- [ ] **Step 3: Separate foreground observation from Remote Control state.**

Keep `inspect_claude_foreground()` as diagnostic/unmanaged-session evidence.
Add `ClaudeRemoteControlState` with `INACTIVE`, `ACTIVE_INCOMPATIBLE`, and
`PROOF_UNAVAILABLE`. Only an integrated structured initialization/capability
event may report `ACTIVE_INCOMPATIBLE`. Ambient foreground presence and proof
unavailability do not become a disconnect requirement.

- [ ] **Step 4: Remove the disconnect-approval escape hatch.**

Remove `--allow-remote-control-disconnect` from `use`, dashboard intents,
control payloads, and provider services. No replacement flag permits stopping
a session. A proven incompatible participant fails prevalidation before native
mutation and remains alive.

- [ ] **Step 5: Adapt official native selection to the epoch port.**

`prevalidate()` proves the protected target and acquires no persistent token.
`commit()` invokes the existing official activation transaction, requires
identity/generation/native propagation evidence, and returns
`AuthorityReadyProof`. Integrated structured participants still receive the
target access lease in Task 8; ambient native sessions may converge next turn
but do not count as ready acknowledgements.

Preserve the existing Linux/WSL mtime proof and macOS qualified propagation
case in the same activation parameter table. Add no separate platform test.

- [ ] **Step 6: Run focused Claude and daemon tests.**

```bash
uv run pytest tests/credentials/claude/test_activation.py \
  tests/providers/claude/test_managed_boundaries.py \
  tests/daemon/test_selection.py -q
uv run ty check src/sidekick_usages/providers/claude \
  src/sidekick_usages/credentials/claude \
  src/sidekick_usages/daemon/worker/claude
```

- [ ] **Step 7: Commit the corrected native behavior.**

```bash
git add src/sidekick_usages/providers/claude \
  src/sidekick_usages/credentials/claude \
  src/sidekick_usages/daemon/worker/claude \
  src/sidekick_usages/cli tests/credentials/claude \
  tests/providers/claude/test_managed_boundaries.py
git commit -m "fix(claude): preserve native next-turn switching"
```

### Task 7: Qualify Claude Structured Control and Token Updates

**Files:**

- Create: `src/sidekick_usages/providers/claude/structured/__init__.py`
- Create: `src/sidekick_usages/providers/claude/structured/models.py`
- Create: `src/sidekick_usages/providers/claude/structured/codec.py`
- Create: `src/sidekick_usages/providers/claude/structured/process.py`
- Create: `src/sidekick_usages/providers/claude/structured/session.py`
- Modify: `src/sidekick_usages/providers/claude/managed/executable.py`
- Modify: `src/sidekick_usages/providers/claude/environment.py`
- Modify: `src/sidekick_usages/providers/claude/process.py`
- Modify: `tests/providers/claude/test_managed_boundaries.py`
- Modify: `tests/fakes/claude/managed.py`

**Interfaces:**

- Consumes: Task 4 readiness/adoption models and current protected Claude
  access-lease owner.
- Produces: `ClaudeStructuredSession.update_oauth()`, the strictly local
  `ClaudeStructuredInstallReceipt`, and an exact behavioral
  `ClaudeStructuredCapability`. It does not produce Task 4 READY or adoption.

- [ ] **Step 1: Add one strict codec and lifecycle proof.**

```python
def test_structured_session_updates_oauth_only_between_turns() -> None:
    session, engine = structured_session()
    session.begin_turn(TURN_ID, EPOCH_N)

    with pytest.raises(ClaudeStructuredError):
        session.update_oauth(lease_b(), EPOCH_N_PLUS_ONE)

    session.end_turn(TURN_ID)
    receipt = session.update_oauth(lease_b(), EPOCH_N_PLUS_ONE)

    assert engine.requests == [oauth_update_frame(REQUEST_ID)]
    assert receipt.request_id == REQUEST_ID
    assert SECRET_CANARY not in repr(session)
    assert SECRET_CANARY not in repr(receipt)
```

Extend the same test with mismatched, replayed, oversized, malformed, timeout,
and error responses by parameterizing response bytes. Keep it one protocol
boundary test. Extend the existing target-prevalidation table, not this
lifecycle test, with expired, rejected, malformed, and transient authorities
so those states remain distinct `SelectionCode` values.

- [ ] **Step 2: Run the focused provider test and verify it fails.**

```bash
uv run pytest \
  tests/providers/claude/test_managed_boundaries.py -q
```

- [ ] **Step 3: Implement the exact bounded structured envelope.**

Write this one-line JSON request to the private child pipe:

```json
{
  "type": "update_environment_variables",
  "request_id": "<uuid>",
  "variables": {
    "CLAUDE_CODE_OAUTH_TOKEN": "<lease>"
  }
}
```

The encoder emits that object as one compact JSON line.

Accept only this correlated success shape from 2.1.220:

```json
{
  "type": "control_response",
  "response": {
    "subtype": "success",
    "request_id": "<uuid>"
  }
}
```

Reject an unexpected top-level envelope, wrong subtype, missing nested
response,
wrong request ID, duplicate response, invalid UTF-8, multiple JSON values, and
frames above the shared bound. Reject extra success fields because the
capability is pinned to the exact envelope above. The secret-bearing mutable
frame is cleared immediately after the pipe write and never enters an
exception.

- [ ] **Step 4: Own the official engine process without adding a TTY shim.**

Launch the resolved executable with:

```text
claude --print --input-format stream-json --output-format stream-json
```

Preserve safe user arguments allowed by the structured host. Use stdin/stdout
pipes and bounded stderr for the child. `ClaudeTerminalHost` alone retains the
original terminal descriptors and translates typed input/events. Preserve
exact process identity and ordinary exit status. Never restart the child on a
protocol or selection failure.

- [ ] **Step 5: Add a no-network behavioral capability probe.**

For an isolated probe child, use a synthetic invalid OAuth canary, send the
allowed update frame, and require the exact success response. Send one
malformed variables object with a non-string value and require rejection with
no success. Do not expect non-allowlisted string keys to return an error: the
installed handler logs refusal but still acknowledges the frame. Prove the
two-key allowlist from the sanitized manifest bound to version `2.1.220`, exact
executable filesystem identity, and SHA-256. Submit no user turn and make no
inference request. A changed launcher, executable, handler envelope, or
manifest requires requalification; mismatch disables setup-token selection
without killing an existing session.

- [ ] **Step 6: Enforce idle-only update and readiness proof.**

`ClaudeStructuredSession` tracks visible turns, background agents, permission
and dialog requests, hooks, tools, MCP operations, and terminal children from
strict events. `update_oauth()` is legal only when all are idle and returns a
`ClaudeStructuredInstallReceipt` after the exact correlated local response.
The receipt proves neither account identity nor provider acceptance. Task 8
binds it to provider proof before separate READY and genuine-turn adoption.

- [ ] **Step 7: Run codec, redaction, type, and architecture checks.**

```bash
uv run pytest \
  tests/providers/claude/test_managed_boundaries.py \
  tests/daemon/test_selection.py -q
uv run ruff check src/sidekick_usages/providers/claude/structured \
  tests/providers/claude/test_managed_boundaries.py
uv run ty check src/sidekick_usages/providers/claude/structured \
  tests/providers/claude/test_managed_boundaries.py
uv run python packaging/check_architecture.py
```

- [ ] **Step 8: Commit exact in-process authority updates.**

```bash
git add src/sidekick_usages/providers/claude \
  tests/providers/claude/test_managed_boundaries.py \
  tests/fakes/claude/managed.py
git commit -m "feat(claude): qualify structured oauth updates"
```

### Task 8: Add the Release-Disabled Claude Protected Plane

**Files:**

- Create: `src/sidekick_usages/credentials/claude/authority/access_lease.py`
- Create: `src/sidekick_usages/providers/claude/structured/data_plane.py`
- Create: `src/sidekick_usages/cli/session/claude.py`
- Modify: `src/sidekick_usages/credentials/claude/authority/resolver.py`
- Modify: `src/sidekick_usages/credentials/claude/activation/service.py`
- Modify: `src/sidekick_usages/providers/claude/structured/models.py`
- Modify: `src/sidekick_usages/providers/claude/structured/codec.py`
- Modify: `src/sidekick_usages/providers/claude/structured/process.py`
- Modify: `src/sidekick_usages/providers/claude/structured/session.py`
- Modify: `src/sidekick_usages/core/selection/types.py`
- Modify: `src/sidekick_usages/core/selection/operation.py`
- Modify: `src/sidekick_usages/daemon/control/client.py`
- Modify: `src/sidekick_usages/daemon/control/protocol.py`
- Modify: `src/sidekick_usages/daemon/control/server.py`
- Modify: `src/sidekick_usages/daemon/control/dispatch.py`
- Modify: `src/sidekick_usages/daemon/selection/models.py`
- Modify: `src/sidekick_usages/daemon/selection/ports.py`
- Modify: `src/sidekick_usages/daemon/selection/registry.py`
- Modify: `src/sidekick_usages/daemon/selection/coordinator.py`
- Modify: `src/sidekick_usages/daemon/selection/recovery.py`
- Modify: `src/sidekick_usages/daemon/selection/worker.py`
- Modify: `src/sidekick_usages/daemon/worker/exchange.py`
- Modify: `src/sidekick_usages/daemon/worker/selection.py`
- Modify: `src/sidekick_usages/daemon/worker/claude/selection.py`
- Modify: `src/sidekick_usages/daemon/runtime/scheduler.py`
- Modify: `src/sidekick_usages/daemon/types/ports.py`
- Modify: `src/sidekick_usages/entrypoints/worker.py`
- Modify: `src/sidekick_usages/entrypoints/supervisor.py`
- Modify: `src/sidekick_usages/cli/commands/session.py`
- Modify: `src/sidekick_usages/cli/session/launcher.py`
- Modify: `src/sidekick_usages/cli/contexts/session.py`
- Modify: `tests/fakes/claude/managed.py`
- Modify: `tests/credentials/claude/test_activation.py`
- Modify: `tests/providers/claude/test_managed_boundaries.py`
- Modify: `tests/daemon/selection/test_coordination.py`
- Modify: `tests/daemon/test_runtime.py`
- Modify: `tests/daemon/test_control.py`
- Modify: `tests/cli/test_sessions.py`

No new test module is permitted. The two new infrastructure owners have
distinct credential and provider-runtime boundaries; they are not generic
token, broker, transport, or compatibility frameworks. The Claude session
host remains the already-approved public command owner.

**Interfaces:**

- Consumes: Task 7's `ClaudeStructuredInstallReceipt`, Task 6's official native
  adapter, Task 4's secret-free participant protocol, and the existing bounded
  worker exchange.
- Produces: `ClaudeSelectedAccessLeaseService`, `ClaudePreparedAuthority`,
  `ClaudeAccessLease`, `ClaudeParticipantChannelRegistry`,
  `ClaudeProtectedCommitRelay`, and the bounded
  `CLAUDE_PARTICIPANT_BIND` operation.
- Extends: the existing `OperationExchangePreparer` composition with one
  child-aware `SelectionWorkerExchangeOwner` injected into
  `SelectionWorkerGateway`. The composite selects Claude or Codex ownership by
  provider and kind; it does not make `CodexRuntimeBroker` a generic broker.
- Preserves: `ParticipantReadyRequest` and adoption as separate secret-free
  control messages. Neither schema contains a protected value.
- Release result: the protected plane and host remain disabled. Public
  selection returns a visible typed unavailable result until every Step 8 gate
  passes.

- [ ] **Step 1: Extend the first two consolidated journeys.**

Journey 1, native and mixed continuity, extends the existing same-engine
structured journey with setup A to setup B and setup B to exact committed
refreshable C. Its lease arrives through a fake protected frame rather than a
caller-owned string.

Journey 2, security and forward recovery, extends the existing
three-participant coordinator schedule. It includes one wrong or replayed
binding and one partial target acknowledgement. Together the journeys prove:

```python
assert journey.engine_process_ids == [ENGINE_PID]
assert journey.conversation_ids == [CONVERSATION_ID]
assert journey.setup_native_mutations == 0
assert journey.refreshable_order == ["native_proof", "protected_install"]
assert journey.queued_prompt_submission_counts == [1, 1]
assert journey.install_before_ready is True
assert journey.next_turn_accounts == [SETUP_B_ID, NATIVE_C_ID]
assert journey.interruptions == []
```

The security/recovery journey also proves every secret canary is absent from
control, persistence, worker results, argv, files, logs, diagnostics,
exceptions, representations, child environments, and immutable CLI strings.
Do not add a matrix, helper test, snapshot, duplicate provider journey, new
fake module, process-helper test, or coverage-padding case.

- [ ] **Step 2: Run the two journeys and verify the protected seam is absent.**

```bash
uv run pytest \
  tests/daemon/selection/test_coordination.py::\
test_three_participants_switch_without_interrupting_turns \
  tests/providers/claude/test_managed_boundaries.py::\
test_structured_session_updates_oauth_only_at_an_idle_turn_boundary -q
```

Expected: fail because no Claude protected channel, initial bind, or mixed
authority owner exists. Do not add another RED test for the same absence.

- [ ] **Step 3: Implement exact target authority ownership.**

`ClaudeSelectedAccessLeaseService` classifies the saved target under held
`ProviderMutationAuthority`. Setup prevalidation validates health, expiry, and
generation, and setup commit performs zero native mutation. Refreshable commit
completes and proves official native activation before opening a lease from
that exact committed native generation. The generic durable-selection resolver
must not choose the old account while the operation is `AWAITING_READY`.

The service returns only safe mode/identity/generation metadata outside its
context-managed mutable lease. It never synthesizes profile, organization,
connector, scope, Remote Control, or refresh capability from a setup token.

- [ ] **Step 4: Add one protected capability socket per participant.**

The Claude host creates one AF_UNIX socketpair. During peer-proven
registration, `sendmsg`/`recvmsg` transfers exactly one non-inheritable
supervisor endpoint with `SCM_RIGHTS`. Bind it to participant ID, connection
generation, and kernel-proven process-start identity. Reject missing,
duplicate, truncated, wrong-type, stale, or replayed descriptors before
membership commit.

`SelectionCoordinator.register()` is the single attachment transaction owner.
It first validates both `ParticipantRegistry` membership and the staged
`ClaudeParticipantChannelRegistry` endpoint. It then commits both or neither.
`ControlConnection` or its dedicated attachment reader owns the received
descriptor through peer verification and strict control decode. It closes the
descriptor on every pre-handoff failure and transfers ownership exactly once
to `SelectionCoordinator` with the verified attachment request. The
coordinator transaction closes it on every later registry, persistence, or
commit failure. The control schema carries only participant ID, connection
generation, client kind, and capability version; it never carries a
credential.

The serialized attachment remains secret-free. Detach the endpoint from the
generic control transport immediately. `ClaudeParticipantChannelRegistry`
owns at most the existing 16 Claude participants and closes only the exact old
endpoint on proved disconnect/reconnect. `SelectionCoordinator` removes or
replaces membership and channel in the same transaction. A disconnected
obligation remains selection-blocking until reconnect or proved death, but no
required live participant remains without its exact protected channel and no
orphan channel survives. Add no public listener, thread, executor, or poller.

- [ ] **Step 5: Reuse the worker exchange for one protected projection.**

Enable the existing exchange for Claude selection commit, recovery-forward,
and participant bind. The worker revalidates the exact target and generation,
opens one bounded mutable lease, writes one operation/account/generation/
epoch/nonce-bound projection, persists only safe completion, releases all
provider/account authorities, and exits. Only then may the resident Claude
relay fan out separately encoded mutable copies and await install receipts.

`SelectionWorkerGateway._operation()` creates the durable child operation ID.
Before `_submit()` enqueues that child, the gateway calls its injected
`SelectionWorkerExchangeOwner` with the exact child operation ID, parent
selection operation ID, provider, and kind. The composite dispatcher selects:

- `ClaudeProtectedCommitRelay` for Claude selection commit,
  recovery-forward, and `CLAUDE_PARTICIPANT_BIND`; or
- the existing Codex preparation owner for only its explicit Codex kinds,
  delegating those matches to `CodexRuntimeBroker.prepare_operation()`.

Claude work never reaches `CodexRuntimeBroker`. Keep
`operation_requires_provider_preparation()` explicit for the Codex-owned
prelaunch cases; exchange presence alone is not a dispatch predicate.

After enqueue, the calling relay may read the one-way protected reply, but it
must wait for scheduler-confirmed successful completion before fan-out.
`DurableScheduler` and `WorkerPool` remain the sole scheduler/executor lane.
`WorkerPool.complete_exchange()` or `WorkerPool.cancel_exchange()` closes the
exact child exchange, and the gateway hook aborts that same child on enqueue,
wakeup, relay, waiter, cancellation, or failure paths. Do not add an executor,
thread, polling loop, worker lane, or generic broker.

Worker results, operation records, journals, control messages, and event state
remain credential-free. Clear the worker, relay, host, and child-encoder
buffers at their owning boundaries. Never retry an ambiguous private update.

- [ ] **Step 6: Keep membership sealed and recover by composite evidence.**

Seal the final required membership through protected distribution and provider
proof. A precommit join is included before mutation or remains behind the old
epoch. After target proof, a late or restarted host uses
`CLAUDE_PARTICIPANT_BIND` against the pending/finalized target before READY or
a real prompt.

Claude recovery returns only `BASELINE_PROVEN`, `TARGET_PROVEN`, or
`UNRESOLVED`. It combines target authority mode, safe worker proof, native
readback, and each exact host's secret-free binding query. Native baseline is
expected for a setup target and is never sufficient rollback proof. Any target
binding forces fresh-lease forward repair. Never retain/replay a lease or send
the old credential as rollback.

- [ ] **Step 7: Build the host behind the disabled capability.**

Add typed bounded interactive send/read operations to the existing structured
process and extend only `ClaudeStructuredEngineFake`. Wire the public Claude
command through the existing typed session runner. The host owns one event
loop for terminal input, structured I/O, and protected channel readiness. It
never imports credential persistence, resolver, private path, or mutation
authority.

The host must preserve one engine PID and conversation while handling the
representative release journey: streaming, permission/question,
tool/hook/MCP/background state, queued input, resize, restoration, positive and
negative private probes, and unqualified-build refusal. Enable qualified child
credential scrubbing. Intercept integrated `/login` through the saved-account
chooser and refuse uncoordinated credential lifecycle commands.

Do not claim a stock TUI, invent provider schemas from the fake, or add an
Agent SDK unless a separate bounded proof shows that it removes owned code
without adding a second wrapper.

- [ ] **Step 8: Hold the setup/mixed release gate closed.**

The exact-build host qualification is the third and final Claude journey. It
is controlled release evidence, not an ordinary test tree. Setup/mixed
selection remains unavailable until all of these pass together:

1. exact artifact, schema, allowlist, and positive/negative private probes;
2. no update during any active turn, retry, tool, hook, permission, dialog,
   MCP operation, task, or terminal operation;
3. unchanged engine PID and conversation across setup A to B and B to C;
4. exact account, generation, epoch, install, READY, and genuine-turn proof;
5. complete parity for every interactive behavior the host exposes;
6. no secret outside bounded protected buffers;
7. forward-only recovery after any target acknowledgement;
8. fail-closed unknown/ambiguous behavior and honest unmanaged status; and
9. written Anthropic product/legal clarification or approval.

Until then, dashboard Enter and scripted selection return a visible typed
unavailable or degraded result without native or structured mutation. Usage,
maintenance, and saved-account visibility remain available.

- [ ] **Step 9: Run the two green journeys, then the static gates.**

```bash
uv run pytest \
  tests/daemon/selection/test_coordination.py::\
test_three_participants_switch_without_interrupting_turns \
  tests/providers/claude/test_managed_boundaries.py::\
test_structured_session_updates_oauth_only_at_an_idle_turn_boundary -q
uv run ruff check src/sidekick_usages/credentials/claude \
  src/sidekick_usages/providers/claude \
  src/sidekick_usages/daemon src/sidekick_usages/cli tests
uv run ty check src/sidekick_usages/credentials/claude \
  src/sidekick_usages/providers/claude \
  src/sidekick_usages/daemon src/sidekick_usages/cli tests
uv run python packaging/check_architecture.py
```

Both exact nodes must pass after implementation and before static gates or
commit. The security journey includes the atomic registration/unwind and exact
child-exchange cleanup assertions. Add no test, matrix, snapshot, helper, fake,
or duplicate journey for these seams.

- [ ] **Step 10: Commit the release-disabled protected plane.**

```bash
git add src/sidekick_usages/credentials/claude \
  src/sidekick_usages/providers/claude \
  src/sidekick_usages/core/selection src/sidekick_usages/daemon \
  src/sidekick_usages/entrypoints src/sidekick_usages/cli \
  tests/credentials/claude tests/providers/claude tests/daemon \
  tests/cli/test_sessions.py tests/fakes/claude/managed.py
git commit -m "feat(claude): add disabled protected selection plane"
```

### Task 9: Pin the Codex Neutral Runtime to Direct HTTP Auth

**Files:**

- Create: `src/sidekick_usages/providers/codex/session/__init__.py`
- Create: `src/sidekick_usages/providers/codex/session/models.py`
- Create: `src/sidekick_usages/providers/codex/session/config.py`
- Create: `src/sidekick_usages/providers/codex/session/home.py`
- Modify: `src/sidekick_usages/providers/codex/app_server/models.py`
- Modify: `src/sidekick_usages/providers/codex/app_server/capabilities.py`
- Modify: `src/sidekick_usages/providers/codex/app_server/methods.py`
- Modify: `src/sidekick_usages/providers/codex/broker/daemon.py`
- Modify: `src/sidekick_usages/providers/codex/broker/service.py`
- Modify: `src/sidekick_usages/entrypoints/supervisor.py`
- Modify: `src/sidekick_usages/paths.py`
- Modify: `src/sidekick_usages/persistence/private/credentials.py`
- Modify: `tests/fakes/codex/app_server/daemon.py`
- Modify: `tests/fakes/codex/app_server/schema.py`
- Modify: `tests/fakes/codex/broker/runtime.py`
- Test: `tests/providers/codex/test_app_server.py`
- Test: `tests/credentials/codex/test_broker.py`

**Interfaces:**

- Consumes: current exact app-server capability probe, resident daemon, neutral
  home path, JSON-RPC client, and external-auth projection.
- Produces: `CodexSessionCapability` and a proven token-free HTTP-only session
  configuration.

- [ ] **Step 1: Add one effective-config and transport proof.**

```python
def test_neutral_runtime_requires_current_auth_without_model_websockets(
    tmp_path: Path,
) -> None:
    runtime = qualified_codex_runtime(tmp_path, version="0.146.0")

    capability = runtime.qualify_session_transport()

    assert capability.model_provider == "sidekick-chatgpt-http"
    assert capability.requires_openai_auth is True
    assert capability.supports_websockets is False
    assert capability.base_url == "https://chatgpt.com/backend-api/codex"
    assert not (runtime.codex_home / "auth.json").exists()
```

Parameterize the same test for wrong version, overridden provider, wrong base,
missing OpenAI auth, and WebSockets enabled. Avoid separate field tests.

- [ ] **Step 2: Run the focused Codex tests and verify qualification fails.**

```bash
uv run pytest tests/providers/codex/test_app_server.py \
  tests/credentials/codex/test_broker.py -q
```

- [ ] **Step 3: Build one protected neutral-home configuration.**

Exact Codex 0.146.0 source proves that the daemon lifecycle client does not
forward `-c` overrides to the detached app server. Before daemon startup,
atomically prepare owner-only neutral-home `config.toml` with these exact TOML
values:

```text
model_provider = "sidekick-chatgpt-http"

[model_providers.sidekick-chatgpt-http]
name = "OpenAI"
base_url = "https://chatgpt.com/backend-api/codex"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
```

`CodexSessionConfig` preserves valid unrelated settings and rejects malformed
TOML or protected-key collisions. Project or alternate-user origins cannot
override these keys. Project the validated provider-owned native `packages`
tree into the neutral home because the official daemon resolves only
`packages/standalone/current/codex`; never project `auth.json`. The neutral
home is not any account's private authority home.

- [ ] **Step 4: Extend the exact 0.146.0 schema probe.**

Require `config/read`, `modelProvider/capabilities/read`, turn
start/completion,
realtime start/closed, external-auth login/notifications, account readback,
and MCP status schemas. Use `config/read` on the resident connection to prove
the effective provider object; never infer it from the planned argv alone.
Keep broader maintenance support floors separate from the exact interactive
selection capability.

- [ ] **Step 5: Move only the interactive resident runtime to neutral home.**

Compose `CodexSharedRuntime` with `paths.codex_session_home`. Keep each saved
account's existing private home as its authority/refresh/usage owner. Do not
copy native/default-home auth or settings. If required non-secret settings are
absent, return `SESSION_CONFIGURATION_REQUIRED` with a dry-run preparation
report.

- [ ] **Step 6: Prove no Responses WebSocket can open.**

The fake app server records model transport selection. Require direct HTTP and
per-attempt current-auth resolution after an external-auth change. A WebSocket
attempt or cached-auth attempt makes `CodexSessionCapability.supported` false
and blocks selection before mutation.

- [ ] **Step 7: Run Codex, type, and architecture gates.**

```bash
uv run pytest tests/providers/codex/test_app_server.py \
  tests/credentials/codex/test_broker.py \
  tests/credentials/codex/test_activation.py -q
uv run ty check src/sidekick_usages/providers/codex \
  src/sidekick_usages/entrypoints/supervisor.py
uv run python packaging/check_architecture.py
```

- [ ] **Step 8: Commit the direct HTTP runtime.**

```bash
git add src/sidekick_usages/providers/codex \
  src/sidekick_usages/entrypoints/supervisor.py \
  src/sidekick_usages/paths.py tests/providers/codex \
  tests/credentials/codex
git commit -m "feat(codex): qualify the http-only session runtime"
```

### Task 10: Add the Codex Admission Relay and Safe Runtime Gates

**Files:**

- Create: `src/sidekick_usages/providers/codex/session/relay.py`
- Modify: `src/sidekick_usages/providers/codex/session/models.py`
- Modify: `src/sidekick_usages/providers/codex/app_server/jsonrpc/codec.py`
- Modify: `src/sidekick_usages/providers/codex/app_server/capabilities.py`
- Modify: `src/sidekick_usages/providers/codex/broker/service.py`
- Modify: `src/sidekick_usages/providers/codex/broker/responder.py`
- Create: `src/sidekick_usages/cli/session/codex.py`
- Modify: `src/sidekick_usages/cli/session/launcher.py`
- Modify: `src/sidekick_usages/cli/contexts/session.py`
- Modify: `tests/fakes/codex/app_server/daemon.py`
- Modify: `tests/fakes/codex/app_server/executable.py`
- Modify: `tests/fakes/codex/app_server/schema.py`
- Modify: `tests/credentials/codex/test_broker.py`
- Modify: `tests/providers/codex/test_app_server.py`
- Modify: `tests/cli/test_sessions.py`

**Interfaces:**

- Consumes: Task 9 resident HTTP-only runtime, existing websockets transport,
  strict JSON-RPC codec, and Task 4 participant gate.
- Produces: one owner-only relay per stock TUI and a Codex
  `SelectionAuthorityAdapter` around existing external auth.

- [ ] **Step 1: Write one relay boundary journey.**

The journey starts one stock fake TUI, admits A, queues a second `turn/start`,
completes A, installs B, opens B, and proves that the client/upstream sockets,
thread ID, `previous_response_id`, approvals, and background terminal IDs are
unchanged. Add one active realtime lease that delays selection until the
natural `thread/realtime/closed` notification.

```python
assert journey.downstream_connections == 1
assert journey.upstream_connections == 1
assert journey.turn_epochs == [EPOCH_N, EPOCH_N_PLUS_ONE]
assert journey.realtime_actions == []
assert journey.account_mutation_requests == [SIDEKICK_OPERATION_ID]
```

- [ ] **Step 2: Run the three focused owner tests and verify relay absence.**

```bash
uv run pytest tests/credentials/codex/test_broker.py \
  tests/providers/codex/test_app_server.py \
  tests/cli/test_sessions.py -q
```

- [ ] **Step 3: Implement a control-only per-participant relay.**

Use websockets 16.1.1 Unix serving and the existing JSON-RPC decoder. Preserve
raw frames only in bounded memory while forwarding. Intercept `turn/start` and
`thread/realtime/start` for admission; observe `turn/started`,
`turn/completed`, `thread/realtime/started`, and
`thread/realtime/closed` for leases. Never inspect user content beyond strict
method/ID/thread/turn routing fields, log bodies, persist frames, change the
backend, or proxy Responses model traffic.

- [ ] **Step 4: Reject uncoordinated account mutation.**

The relay returns a typed JSON-RPC refusal for `account/login/start`,
`account/login/cancel`, and `account/logout` from the TUI, with guidance to the
Sidekick saved-account/credential commands. The coordinator's authenticated
resident mutation path remains allowed under one operation ID.

- [ ] **Step 5: Bind external-auth proof to the pending epoch.**

Reuse current protected projection, local access-token account claim,
serialized resident mutation lock, null-`loginId` success, ordered
`account/updated`, plan readback, and secret-free receipt. Add pending epoch
and
qualified socket identity. Do not claim that `account/read` echoes a provider
account ID.

Bind refresh callbacks to the pending/finalized epoch and the matching private
`CODEX_HOME`. Extend the existing broker callback journey so a stale callback
cannot install an older lease and another account's home cannot answer it.

- [ ] **Step 6: Gate realtime and account-scoped cache refresh.**

Realtime start is a turn lease; selection sends no stop or close. For plugin,
skill, and MCP invalidation, qualify an observable completion sequence on exact
0.146.0 using account update plus the loaded-thread MCP status notifications
and subsequent strict `mcpServerStatus/list` readback. Retain exact per-thread
server names and status revisions before mutation; after mutation, require
every configured server to reach a later `ready` revision. A failed or
cancelled server is terminal for precommit drain but cannot silently finalize
ordinary READY. Reread the same names before READY and OPEN.

The local proof protocol must distinguish mutation proof from late-participant
readback binding. A late or reconnected participant has no second provider
mutation to trigger another MCP refresh; require unchanged threads, names, and
terminal states instead. If the mutation path cannot prove that every loaded
thread applied its queued refresh, fail closed before OPEN. The bounded wait is
only a failure deadline and never evidence of success.

When READY completed but durable finalization enters recovery, retain the proof
only for the same target epoch, loaded-thread revision, confirmed MCP names,
and still-proven state. Keep admission closed and reuse that proof for the
coordinator's later OPEN. Every precommit, unready, changed, or mismatched
recovery path discards it.

Discard a failed POSTCOMMIT proof before coordinator abort so the previous
authority can resume. Treat repeated OPEN for an already finalized epoch as
idempotent, but commit an uncommitted same-epoch newer-generation target first.

- [ ] **Step 7: Start stock Codex once through the stable relay.**

`session codex` registers, starts the relay, and invokes:

```text
codex --remote unix://<owner-only-participant-socket> [USER_ARGUMENTS...]
```

Use the neutral `CODEX_HOME`, preserve cwd/TTY/signals/exit status, and keep
the
relay plus resident connection for the TUI lifetime. Selection failure leaves
the TUI and resident server alive on the last proven epoch.

- [ ] **Step 8: Keep large current owners from growing.**

`CodexRuntimeBroker` delegates new session readiness to the new relay/session
owner. Do not add relay parsing or gate state to the current 826-line
responder.
If a small existing method must move, preserve its public behavior and reduce
the responder's line count in the same commit.

- [ ] **Step 9: Run relay, broker, coordinator, type, and architecture gates.**

```bash
uv run pytest tests/credentials/codex/test_broker.py \
  tests/providers/codex/test_app_server.py \
  tests/cli/test_sessions.py tests/daemon/test_selection.py -q
uv run ty check src/sidekick_usages/providers/codex/session \
  src/sidekick_usages/providers/codex/broker \
  src/sidekick_usages/cli/session/codex.py
uv run python packaging/check_architecture.py
```

- [ ] **Step 10: Commit the stable Codex participant boundary.**

```bash
git add src/sidekick_usages/providers/codex \
  src/sidekick_usages/cli/session tests/credentials/codex \
  tests/providers/codex tests/cli/test_sessions.py
git commit -m "feat(codex): gate turns without reconnecting sessions"
```

### Task 11: Wire Dashboard and Scripted Selection to the Coordinator

**Files:**

- Modify: `src/sidekick_usages/cli/commands/use.py`
- Modify: `src/sidekick_usages/cli/contexts/use.py`
- Modify: `src/sidekick_usages/cli/dashboard/actions.py`
- Modify: `src/sidekick_usages/cli/dashboard/controller.py`
- Modify: `src/sidekick_usages/cli/dashboard/session.py`
- Modify: `src/sidekick_usages/cli/dashboard/ports.py`
- Modify: `src/sidekick_usages/usage/dashboard/models.py`
- Modify: `src/sidekick_usages/usage/dashboard/service.py`
- Modify: `src/sidekick_usages/usage/presentation/dashboard/render/text.py`
- Modify: `src/sidekick_usages/daemon/control/client.py`
- Modify: `src/sidekick_usages/daemon/control/dispatch.py`
- Test: `tests/dashboard/test_actions.py`
- Test: `tests/dashboard/test_pty.py`
- Test: `tests/cli/test_sessions.py`

**Interfaces:**

- Consumes: both provider adapters and the Task 4 coordinator.
- Produces: one selection operation path shared by dashboard Enter, scripted
  `use`, provider-local chooser, and participant status.

- [ ] **Step 1: Replace activation-only UI tests with one typed selection
  journey.**

Press Enter on Claude B during an A turn, require a visible waiting status,
queue a prompt, complete A, observe ready N+1, and require the footer to show
ready/adoption counts. Repeat the operation call for Codex through the same
controller fake, not a duplicate UI test.

```python
assert statuses == [
    "Preparing account change…",
    "Waiting for 1 active turn…",
    "Account ready in 3 sessions; next requests use it.",
]
assert connector.selection_requests == [TARGET_ACCOUNT_ID]
```

- [ ] **Step 2: Run dashboard and CLI tests and verify the old activation
  route fails.**

```bash
uv run pytest tests/dashboard/test_actions.py \
  tests/dashboard/test_pty.py tests/cli/test_sessions.py -q
```

- [ ] **Step 3: Route every selection surface through one request.**

`use`, dashboard Enter, Claude `/login`, and any saved chooser call
`ControlClient.select(provider_id, account_id)`. Remove direct activation
dispatch from public selection surfaces. Maintenance, refresh, migration, and
credential-creation commands remain separate.

- [ ] **Step 4: Project typed selection state without pseudo-rows.**

Add provider-level status for current target, phase, finalized epoch, required,
ready, adopted, lost, unreachable, and unmanaged counts. Saved rows show exact
capability/refusal state. Normal healthy rows stay uncluttered. A focused saved
row always gives immediate footer feedback on Enter.

- [ ] **Step 5: Preserve stable focus through refresh and resize.**

When account data or terminal dimensions change, keep
`(provider_id, account_id)` if that saved ID remains. Otherwise choose the
verified active saved row or first saved row for that provider. The body window
scrolls it into view; footer status remains fixed.

- [ ] **Step 6: Run dashboard, daemon, and CLI owner suites.**

```bash
uv run pytest tests/dashboard tests/daemon/test_control.py \
  tests/daemon/test_selection.py tests/cli/test_sessions.py \
  tests/usage/test_dashboard_render.py -q
uv run ruff check src/sidekick_usages/cli \
  src/sidekick_usages/usage tests/dashboard tests/cli
```

- [ ] **Step 7: Commit the unified public selection path.**

```bash
git add src/sidekick_usages/cli src/sidekick_usages/usage \
  src/sidekick_usages/daemon/control tests/dashboard \
  tests/cli/test_sessions.py tests/usage/test_dashboard_render.py
git commit -m "feat(selection): route account choices through epochs"
```

### Task 12: Preserve Maintenance and Repair Platform Diagnostics

**Files:**

- Modify: `src/sidekick_usages/daemon/lifecycle/platform/selection.py`
- Modify: `src/sidekick_usages/daemon/lifecycle/platform/wsl.py`
- Modify: `src/sidekick_usages/daemon/lifecycle/manager.py`
- Modify: `src/sidekick_usages/cli/commands/doctor.py`
- Modify: `src/sidekick_usages/cli/commands/daemon.py`
- Modify: `src/sidekick_usages/doctor/runtime/models.py`
- Modify: `src/sidekick_usages/doctor/runtime/service.py`
- Modify: `src/sidekick_usages/doctor/presentation/json.py`
- Verify unchanged: `src/sidekick_usages/usage/service.py`
- Verify unchanged: `src/sidekick_usages/maintenance.py`
- Modify: `tests/daemon/test_lifecycle.py`
- Modify: `tests/doctor/test_command_states.py`
- Modify: `tests/doctor/test_reporting.py`
- Modify: `tests/usage/test_check.py`
- Modify: `tests/heartbeat/test_maintenance.py`
- Modify: `tests/heartbeat/test_service.py`

**Interfaces:**

- Consumes: final coordinator/session capability snapshots and existing
  provider-neutral maintenance scheduling.
- Produces: accurate doctor/status reporting and unchanged all-account usage
  plus freshness behavior.

- [ ] **Step 1: Extend one lifecycle matrix and one maintenance proof.**

The existing platform matrix must show that Linux-side WSL status works with
systemd and no distribution name, while a Windows-side rescue action requires
an explicit distribution. The existing usage service test must select B while
refreshing A and C, then require deterministic saved order and no adoption
side effect.

- [ ] **Step 2: Run focused lifecycle and reporting tests and verify the WSL
  branch fails.**

```bash
uv run pytest tests/daemon/test_lifecycle.py \
  tests/doctor/test_command_states.py tests/doctor/test_reporting.py \
  tests/usage/test_check.py tests/heartbeat/test_maintenance.py \
  tests/heartbeat/test_service.py -q
```

- [ ] **Step 3: Separate Linux-side WSL service control from Windows rescue.**

When running inside WSL, normal install/start/status/stop use the Linux systemd
user backend and distribution-local owner-only IPC. Only a host-side WSL rescue
or dispatch path requires a resolved explicit distribution. Preserve native
Linux and macOS selection logic.

- [ ] **Step 4: Add exact session and selection diagnostics.**

Doctor/status reports supervisor protocol, active recovery, participant counts,
unmanaged sessions, shell integration status, provider capability reason,
neutral Codex effective config, Claude structured qualification, and WSL
backend. Use safe codes and counts only. Never print provider payloads, emails,
tokens, prompt text, raw argv, or participant process details.

- [ ] **Step 5: Reprove selection-independent account maintenance.**

Keep current cached-first bounded concurrent collection. Refreshable Claude and
Codex accounts remain scheduled whether selected or not. Claude setup tokens
receive validation and usage collection but no fake refresh outcome. A single
malformed, rejected, unreadable, or transient account does not cancel others,
and completion always projects persisted order. Do not add a selection branch
to `UsageCheckService`, `MaintenanceService`, heartbeat, or provider refresh.
If the focused proof exposes a pre-existing coupling, fix it only in its
current owner and keep the same public result types.

- [ ] **Step 6: Run reporting and platform gates.**

```bash
uv run pytest tests/daemon/test_lifecycle.py \
  tests/doctor/test_command_states.py tests/doctor/test_reporting.py \
  tests/usage/test_check.py tests/heartbeat/test_maintenance.py \
  tests/heartbeat/test_service.py -q
uv run ty check src/sidekick_usages/daemon/lifecycle \
  src/sidekick_usages/doctor src/sidekick_usages/usage \
  src/sidekick_usages/maintenance.py
uv run python packaging/check_architecture.py
```

- [ ] **Step 7: Commit platform and reporting continuity.**

```bash
git add src/sidekick_usages/daemon/lifecycle \
  src/sidekick_usages/cli/commands/doctor.py \
  src/sidekick_usages/cli/commands/daemon.py \
  src/sidekick_usages/doctor tests/daemon/test_lifecycle.py \
  tests/doctor/test_command_states.py tests/doctor/test_reporting.py \
  tests/usage/test_check.py tests/heartbeat/test_maintenance.py \
  tests/heartbeat/test_service.py
git commit -m "fix(runtime): preserve reporting across session selection"
```

### Task 13: Harden Packaging, Migration, and Controlled Cutover

**Files:**

- Modify: `packaging/check_architecture.py`
- Modify: `packaging/architecture/rules/runtime.py`
- Modify: `packaging/architecture/rules/codex.py`
- Modify: `packaging/smoke_wheel.py`
- Modify: `README.md`
- Modify: `docs/claude/README.md`
- Modify: `docs/claude/debugging.md`
- Modify: `docs/codex/README.md`
- Modify: `docs/heartbeat.md`
- Modify: `docs/persistence-and-recovery.md`
- Modify: `tests/test_architecture.py`
- Modify: `tests/test_docs.py`
- Modify: `tests/test_packaging.py`
- Modify: `tests/test_smoke.py`
- Modify: `tests/daemon/test_selection.py`
- Modify: `tests/cli/test_sessions.py`

**Interfaces:**

- Consumes: all previous task outputs.
- Produces: enforced ownership, a releasable wheel, a safe current-machine
  migration, operator documentation, and final evidence.

- [ ] **Step 1: Add only architecture assertions that prevent real duplicate
  owners.**

Enforce that bootstrap cannot import dashboard rendering or prompt-toolkit,
core selection cannot import CLI/persistence/provider code, provider session
adapters cannot discover application paths, the Codex relay cannot import HTTP
model transport, and only `paths.py` owns new path literals. Do not add one
rule per file or a duplicated dependency graph.

- [ ] **Step 2: Add a wheel smoke journey with synthetic providers.**

Extend the existing external-wheel smoke to run help, one-shot reporting,
dashboard PTY startup/quit, session shell dry-run, and a release-disabled
Claude session. Exercise default Codex session composition with an isolated
`PATH` that contains no provider executable and require the exact prelaunch
missing-executable refusal. It uses isolated XDG/provider homes and never
resolves live credentials or provider binaries.

- [ ] **Step 3: Document exact product and bypass behavior.**

Document saved-only counts, fixed footer/scrolling, explicit session commands,
shell install/uninstall/status, integrated versus unmanaged sessions, provider
capability differences, next-turn account adoption, queued prompts, cross-
account conversation context, no-interruption contract, diagnostics, and the
fact that absolute-path/`command` bypass remains unmanaged and alive.

- [ ] **Step 4: Run the complete automated gate before any live migration.**

```bash
uv run pytest --cov=sidekick_usages
uv run ruff format --check src/ tests/ packaging/
uv run ruff check src/ tests/ packaging/
uv run ty check src/ tests/ packaging/
uv run python packaging/check_architecture.py
uv run pre-commit run --all-files
npm ci
npm audit --audit-level=moderate
npm run lint:markdown
uv build
uv run python packaging/smoke_wheel.py --build
```

Expected: every command succeeds. The built artifacts stay untracked.

- [ ] **Step 5: Verify module size, line length, test value, and secret
  absence.**

```bash
find src/sidekick_usages tests packaging -name '*.py' -print0 | \
  xargs -0 awk '
    length($0) > 79 {
      print FILENAME ":" FNR ":" length($0)
      bad = 1
    }
    END { exit bad }
  '
find src/sidekick_usages -name '*.py' -print0 | \
  xargs -0 wc -l | \
  awk '
    $2 != "total" && $1 > 1000 {
      print
      bad = 1
    }
    END { exit bad }
  '
test -d .agents/tmp/provider-live-evidence
forbidden_evidence='access[_-]?token|refresh[_-]?token|authorization'
forbidden_evidence="${forbidden_evidence}|bearer[[:space:]]|email"
forbidden_evidence="${forbidden_evidence}|prompt|response|argv|command.line"
! rg -n -i "$forbidden_evidence" \
  .agents/tmp/provider-live-evidence
git status --short
```

Review every test added by this plan and delete any case that cannot fail for a
distinct acceptance contract. The evidence directory contains sanitized
metadata only; any credential match blocks cutover.

- [ ] **Step 6: Build a reversible Sidekick-state migration rehearsal.**

On copied synthetic fixtures shaped like the current machine, prove four
Claude IDs and two Codex IDs/private-home relations survive, order remains,
selected state points only to a saved ID, panel counts remain four and two,
and no unrelated authority file content or mode changes. Seed ordinary Codex
provider runtime files in each private home and prove exact config/auth
transactions preserve them. An identity-matching healthy managed authority
must verify without browser login. Rehearse Sidekick schema rollback before
provider-live work; never roll provider credentials backward.

- [ ] **Step 7: Commit the release gate before live qualification.**

```bash
git add packaging README.md docs/claude docs/codex docs/heartbeat.md \
  docs/persistence-and-recovery.md tests/test_architecture.py \
  tests/test_docs.py tests/test_packaging.py tests/test_smoke.py \
  tests/daemon/test_selection.py tests/cli/test_sessions.py
git commit -m "docs(selection): complete hardened rollout gates"
```

- [ ] **Step 8: Run controlled provider-live qualification only with explicit
  disposable-account authority.**

For Claude, prove setup A/setup B/native C in one and three integrated
sessions,
ordinary native `/login` next-request convergence, streaming-turn drain,
permission/tool/MCP parity, and the same child PID/conversation. For Codex,
prove direct HTTP attempts, no Responses WebSocket, external-auth notification
order/readback, active-turn/realtime drain, MCP/plugin transparency, the same
TUI/app-server/thread/socket, and next-attempt B adoption. Record only redacted
IDs, epochs, generations, counts, process sameness booleans, and safe outcomes.

If Claude parity, Codex MCP quiescence, realtime observation, exact schema, or
provider terms fail, leave that mechanism disabled, keep the installed 0.7.0
reporter untouched, and return the failed gate to design review. Do not ship a
restart, reconnect, proxy, timer, or credential-copy fallback.

- [ ] **Step 9: Perform current-machine cutover only after every gate passes.**

First export a wheel/install manifest and Sidekick-only schema backup with
owner-only permissions. Install the exact qualified wheel, run the CLI
migration, and verify four Claude and two Codex saved accounts plus usage
reporting. Before replacing the supervisor, prove every provider client and
the official Codex daemon are outside its service cgroup. Publish the
`KillMode=process` unit, replace only the supervisor, then prove the same
provider PIDs, sockets, and native Claude metadata survived. Never perform the
replacement with an unresolved ownership topology. Shell enrollment remains a
separate explicit `session shell install`; package migration does not apply it.

Do not assume the old binary can read the new schema after migration. Before
migration, executable rollback keeps the old installation. After migration,
recovery uses the new Sidekick schema/CLI and never restores an older provider
credential generation.

- [ ] **Step 10: Verify live reporting and feature-branch cleanliness.**

Run the installed command's normal reporting surface, `doctor`, shell status,
and synthetic session smoke. Confirm provider counts, deterministic ordering,
fresh observation timestamps, and no external rows. With the WSL variable
explicitly absent, normal reporting must now exit zero without the
compatibility workaround. Then require:

```bash
env -u WSL_DISTRO_NAME sidekick-usages --no-interactive check >/dev/null
git status --short --branch
git log --oneline --decorate origin/develop..HEAD
```

Expected: only reviewed feature-branch commits, no credentials, captures,
builds, caches, or live state tracked.

## 5. Design Traceability

| Approved design section | Implemented by |
| --- | --- |
| 3.2 duplicate logo | Task 2 |
| 3.3 hidden footer | Task 2 |
| 3.4 invalid external rows | Task 1 |
| 3.5 dead selection | Tasks 1 and 11 |
| 3.6 Claude foreground guard | Task 6 |
| 3.7 Codex stale WebSocket | Task 9 |
| 3.8 WSL control failure | Task 12 |
| 4 goals and invariants | Global Constraints and Tasks 4, 8, 10 |
| 5 architecture and enrollment | Tasks 4, 5, 8, 9, 10 |
| 6 authority/state model | Tasks 3 and 4 |
| 7 responsive dashboard | Tasks 1 and 2 |
| 8 no-interruption protocol | Tasks 3 and 4 |
| 9 Claude design | Tasks 6, 7, and 8 |
| 10 Codex design | Tasks 9 and 10 |
| 11 freshness and reconciliation | Task 12 |
| 12 persistence and recovery | Tasks 3, 4, 8, and 13 |
| 13 security boundaries | Global Constraints and Tasks 3 through 13 |
| 14 diagnostics/platform | Task 12 |
| 15 repository ownership | File Map and Task 13 |
| 16 verification gates | Each task's focused proof and Task 13 |
| 17 delivery/migration | Execution Preflight and Task 13 |
| 18 build-versus-adopt | Global Constraints and File Map |
| 19 rejected designs | Global Constraints and Tasks 6 through 10 |
| 20 revalidation triggers | Tasks 7, 9, 10, and 13 |
| 21 sources | Normative tracked design; no scratch dependency |
| 22 review checklist | Section 6 below |

## 6. Plan Review Checklist

Run this review after drafting implementation changes and again before Task 13
cutover. Correct gaps in the feature branch; do not waive them.

- [ ] Every implementation file has one owner in Section 2.
- [ ] Every produced cross-task type has one defining task and identical later
  spelling/signature.
- [ ] Every `Modify`, `Test`, and `Delete` path exists at the baseline or is a
  prior task's explicit `Create` output.
- [ ] `PreparedSelection.operation_id` originates from the coordinator's open
  operation; no adapter invents transaction identity.
- [ ] Kernel-verified peer evidence reaches participant registration through
  `VerifiedControlRequest`; client payloads never assert process identity.
- [ ] Provider prevalidation, commit, and readback use the existing bounded
  worker/exchange lane and never block the supervisor selector.
- [ ] No task relies on `.agents/tmp` research to explain a product decision;
  the approved tracked design contains the complete research and sources.
- [ ] The UI repair can be reviewed without claiming provider convergence.
- [ ] External runtime state appears only as nonfocusable provider/session
  status, never a row or selected record.
- [ ] Participant prompts and raw provider frames stay in process memory and
  never enter supervisor persistence.
- [ ] A participant joining during selection starts behind the pending gate.
- [ ] Active turns and retries cannot cross an epoch.
- [ ] Post-commit loss yields forward recovery or degraded target, never false
  success or credential rollback.
- [ ] Claude ordinary foreground presence cannot produce a Remote Control
  disconnect requirement.
- [ ] Claude leases use only worker exchange and peer-bound provider channels;
  control, CLI, persistence, results, and receipts remain secret-free.
- [ ] A Claude private response is an install receipt only; provider proof,
  READY, and genuine-turn adoption remain distinct.
- [ ] The Claude 2.1.220 success fixture contains only `subtype` and
  `request_id` inside `response`; no invented empty response object remains.
- [ ] Setup selection performs no native mutation; refreshable selection proves
  native target before projecting the exact committed generation.
- [ ] Claude membership stays sealed through protected distribution; initial
  and late hosts bind the target before READY or a real prompt.
- [ ] Claude recovery uses composite native/participant evidence; native
  baseline alone never proves setup-target rollback.
- [ ] Setup/mixed selection remains visibly unavailable until all exact-build,
  parity, genuine-turn, security, recovery, and written Anthropic gates pass.
- [ ] No Agent SDK, token service, compatibility layer, duplicate transport,
  executor, broker thread, or polling loop duplicates existing owners.
- [ ] Codex model Responses WebSockets are disabled while TUI/app-server
  control sockets remain open.
- [ ] Codex external-auth proof does not overclaim provider identity from
  `account/read`.
- [ ] Codex active tool/MCP/realtime work drains naturally; missing quiescence
  evidence blocks before mutation rather than using a timer.
- [ ] Selection and maintenance remain independent state machines.
- [ ] The installed 0.7.0 command remains available for live metrics until
  final qualified cutover.
- [ ] Until the Task 12 repair reaches the final wheel, the process-local WSL
  reporting compatibility command exits zero and the installed SHA is
  unchanged.
- [ ] Task 8 uses exactly three consolidated Claude journeys and no new test
  modules, matrices, helper tests, snapshots, duplicate journeys, new fake
  modules, process-helper tests, or coverage-padding cases.
- [ ] No code, comment, or docstring added or changed by implementation exceeds
  79 characters.
- [ ] No current module exceeds 1000 lines and no cohesion-heavy module is
  allowed to grow merely to avoid a proper owner.
- [ ] Full static, architecture, test, documentation, build, and wheel gates
  pass before provider-live work.
- [ ] Provider-live work uses disposable accounts and explicit authority, and
  persists only redacted evidence.
- [ ] Current-machine migration preserves four Claude and two Codex saved
  accounts, authorities, private homes, order, usage, and refresh eligibility.
- [ ] Shell enrollment and package/schema migration remain separate explicit
  operations.
- [ ] Implementation commits exist only on
  `feat/hardened-global-account-selection` until reviewed integration.

## 7. Completion Condition

Release is capability-specific. The dashboard, refreshable/native Claude, and
qualified Codex paths may complete independently after their own gates pass.
Claude setup/mixed switching is complete only after all three consolidated
journeys, written Anthropic resolution, controlled live genuine-turn identity,
and every Step 8 gate pass together. Until then, its correct product state is a
visible typed unavailable result with saved-account reporting and maintenance
unchanged.

Every released capability also requires all automated gates, current-machine
migration preserving saved state/reporting, the dashboard correct at all five
critical terminal sizes, and open integrated supported sessions proving
same-process next-turn adoption without interruption.

Passing dashboard tests alone is not account selection. Passing provider auth
readback alone is not cross-session convergence. A mechanism that requires a
restart, reconnect, credential copy, model proxy, or unobservable timer is a
failed gate, not a partial success.

[approved-design]:
  ../specs/2026-08-01-hardened-global-account-selection-design.md

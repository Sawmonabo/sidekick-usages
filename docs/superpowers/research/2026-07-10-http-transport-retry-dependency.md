# HTTP Transport and Retry Dependency Research

- **Status:** **GO — OPERATOR APPROVED 2026-07-10**
- **Date:** 2026-07-10
- **Repository:** `/home/sabossedgh/dev/sidekick-usages`
- **Change set:** CS-08 — decide HTTP transport and retry ownership
- **Evidence commit:** `c5b588ad474fd95c597cfd0b64339223e3da1843`
- **Production impact:** None; this record changes no dependency or runtime
  behavior

This document is the self-contained tracked evidence and decision record for
CS-08. It refreshes the preliminary HTTP decision in the approved architecture,
records the measured comparison, and documents the operator-approved
transport/retry combination. The GO authorizes CS-11 implementation subject to
every retained implementation and release gate.

## Table of Contents

- [Research Question](#research-question)
- [Executive Result](#executive-result)
- [Repository Baseline](#repository-baseline)
- [Standards and Safety Rules](#standards-and-safety-rules)
- [Operation Inventory](#operation-inventory)
- [Complete Retry Matrix](#complete-retry-matrix)
- [Candidate Comparison](#candidate-comparison)
- [Isolated Measurements](#isolated-measurements)
- [Retry-After and Deadline Contract](#retry-after-and-deadline-contract)
- [Pooling TLS CA Proxy and Redirect Boundary](#pooling-tls-ca-proxy-and-redirect-boundary)
- [Typed Error and Security Boundary](#typed-error-and-security-boundary)
- [Packaging Platform License and Maintenance](#packaging-platform-license-and-maintenance)
- [Approved Decision](#approved-decision)
- [Implementation Implications](#implementation-implications)
- [Limitations and Open Risks](#limitations-and-open-risks)
- [Reversal and Stop Conditions](#reversal-and-stop-conditions)
- [Test and Verification Contract](#test-and-verification-contract)
- [Primary Source List](#primary-source-list)

## Research Question

Which pooled HTTP transport and sole retry owner should replace the current
standard-library implementation while preserving Sidekick Usages' four real
request capabilities, typed application errors, final rate-limit guidance,
strict POST safety, deterministic tests, small packaging surface, and Python
3.14 platform support?

The approved comparison requires:

1. urllib3 2.7.0 `PoolManager` with urllib3 `Retry` as owner;
2. retry-disabled urllib3 with Tenacity 9.1.4 as owner; and
3. retry-disabled urllib3 with a focused local executor as owner.

HTTPX 0.28.1 and Stamina 26.1.0 are buy-versus-adopt controls. A non-pooled
standard-library implementation is a measurement baseline but cannot satisfy
the approved pooling requirement.

## Executive Result

The evidence supported, and the operator approved, **GO** for:

- **Transport:** urllib3 2.7.0 `PoolManager`/`ProxyManager`;
- **Retry owner:** one focused local `RetryExecutor` in `http/retry.py`;
- **Transport configuration:** urllib3 retries disabled, plus
  `retries=False` and `redirect=False` on every request; and
- **Lifecycle:** one pooled client per CLI invocation, closed deterministically
  at composition shutdown and never initialized for help/version.

The alternatives are rejected as retry owners:

- urllib3 `Retry` has strong HTTP semantics, but its tagged implementation
  directly owns wall time, sleeping, and randomness and has no
  whole-operation elapsed deadline. Satisfying the approved independent time
  injection would require global patching or a brittle subclass coupled to
  internal reconstruction and sleep methods.
- Tenacity injects sleep and can retain a terminal result, but its retry state
  directly owns `time.monotonic()`. Sidekick would still implement the HTTP
  operation matrix, transport classifier, `Retry-After`, aware wall time, the
  actual deadline, final response bridge, and typed errors. The second package
  would remove little cohesive local code.
- HTTPX and Stamina add capabilities and dependency surface that no current
  call site requires.

This is an **operator-approved decision**. Production dependency and HTTP work
may proceed in CS-11. Release remains conditional on explicit
environment-proxy, system-CA, redirect, native-platform, Homebrew, and
typed-error acceptance.

## Repository Baseline

At the evidence commit, `src/sidekick_usages/http.py` is 469 physical lines.
It already provides one reusable `HttpClient`; a second client service or
generic transport protocol is not justified. The defects are within the
existing infrastructure boundary:

- three retry loops at `http.py:68-125`, `http.py:127-195`, and
  `http.py:275-292`;
- blanket POST retries for 429 and every status from 500 through 599 at
  `http.py:311-336`;
- one float timeout per request rather than separate connect/read timeouts and
  a whole-operation monotonic budget;
- a new `urllib.request.urlopen` operation per attempt rather than one shared
  connection pool;
- unchecked `cast("dict[str, Any]", payload)` after JSON decoding;
- integer-only `Retry-After` parsing at `http.py:438-451`;
- a module-global, non-injectable jitter source; and
- `ValueError` for non-HTTPS input rather than the design-approved typed error.

The production call sites require exactly four transport capabilities:

| Capability | Current consumers |
|---|---|
| GET and decode one JSON object | GitHub release check; Claude usage and inspection; Codex usage and inspection |
| POST a JSON object and decode one JSON object | Claude OAuth refresh fallback |
| POST form data and decode one JSON object | Codex OAuth refresh |
| POST JSON/bytes, drain the body, and return normalized headers | Claude usage probe; Claude heartbeat; Codex heartbeat |

The existing public application errors are `AuthError`, `ForbiddenError`,
`RateLimitError`, and `TransientError`. The approved design additionally calls
for `InsecureUrlError` and one typed invalid-payload error. The selected
transport must remain private to `http/`; no urllib3 or retry-library type may
cross that boundary.

The existing focused HTTP test module passed 10 of 10 cases on Linux CPython
3.14.6. It pins 401, distinct 403 diagnostics, terminal 5xx, JSON POST encoding,
429 retry, and final integer guidance. It does not yet pin HTTP-date guidance,
elapsed deadlines, ambiguous POST safety, pooling/closure, environment proxy,
redirect rejection, body bounds, or wrong-shape JSON.

## Standards and Safety Rules

[RFC 9110 section 9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2)
governs replay safety. A client should not automatically retry a
non-idempotent request unless it knows that the operation is effectively
idempotent or knows that the original request was not applied. A generic
`POST` status loop cannot establish either fact.

urllib3 documents the transport distinction needed by this application:

- a connect error occurs before the request is sent and is assumed not to have
  triggered server processing; and
- a read error occurs after sending and might have side effects.

The tagged implementation makes the same distinction in
`_is_connection_error` and `_is_read_error`
([urllib3 2.7.0 source](https://github.com/urllib3/urllib3/blob/2.7.0/src/urllib3/util/retry.py#L389-L401)).
The local executor should reuse this narrow classification model, not retry
every `HTTPError` or unknown exception.

Credential refresh is the highest-severity case.
[RFC 9700 section 4.14.2](https://www.rfc-editor.org/rfc/rfc9700.html#section-4.14.2)
explains that refresh-token rotation invalidates the previous token and that
replay detection can revoke the active grant. If a provider rotates a token
but the response is lost, replaying the old token can turn a recoverable
network ambiguity into forced reauthentication. Neither refresh operation has
a documented endpoint idempotency key or not-applied signal.

Provider-wide retry defaults are context, not proof. Anthropic documents that
its official SDKs retry connection, rate-limit, and 5xx failures by default
([Anthropic errors](https://platform.claude.com/docs/en/api/errors)). That
policy does not establish idempotency for Sidekick's OAuth exchange or every
tiny inference POST. OpenAI recommends bounded backoff for unsuccessful 429
requests
([OpenAI rate-limit guidance](https://help.openai.com/en/articles/5955604-how-can-i-solve-429-too-many-requests-errors)),
but that public guidance is not an endpoint contract for the private Codex
backend.

## Operation Inventory

The closed operation vocabulary has six concrete members. It is internal to
`http/retry.py`; callers do not supply arbitrary policies or `retry: bool`.

| Closed operation | Concrete request | Possible effect | Idempotency or not-applied evidence | Maximum automatic scope |
|---|---|---|---|---|
| Safe read | GitHub release GET; Claude/Codex usage GET; heartbeat inspection GET | Read only | HTTP GET semantics; no local mutation before validated response | Narrow connect/read failures, documented 429, selected transient statuses |
| Claude usage probe | `POST /v1/messages`, `max_tokens=1`, discard body and read headers | Consumes a tiny inference request/quota | No idempotency key; documented Messages 429 rejection | Proven pre-send connect; documented 429 only |
| Claude refresh | JSON OAuth refresh-token exchange | Can rotate access and refresh credentials | No endpoint idempotency key or not-applied contract found | Proven pre-send connect only |
| Codex refresh | Form OAuth refresh-token exchange | Can rotate access and refresh credentials | No endpoint idempotency key or not-applied contract found | Proven pre-send connect only |
| Claude heartbeat | Same tiny Claude Messages POST | Intentionally starts/warms a usage window and consumes quota | No idempotency key; documented Messages 429 rejection | Proven pre-send connect; documented 429 only |
| Codex heartbeat | `POST /backend-api/codex/responses`, streaming and `store=false`, then usage GET | Intentionally warms a window and consumes quota | `store=false` is not request idempotency; no endpoint retry contract found | Proven pre-send connect only |

Claude probe and heartbeat share a wire shape but remain separate operation
names because their product intent and follow-up behavior differ. They may map
to the same immutable policy today. That does not justify collapsing all POSTs
or adding provider-defined hooks.

## Complete Retry Matrix

Legend:

- **R** — retry within both the total-attempt and monotonic elapsed bounds;
- **R\*** — retry only when a valid full server delay fits the remaining
  elapsed budget; and
- **T** — return one terminal typed application failure without replay.

| Operation | Proven pre-send connect failure | Ambiguous read/protocol failure | HTTP 429 | Selected 5xx | 401, 403, or other explicit rejection |
|---|---:|---:|---:|---:|---:|
| Safe read | R | R | R\* | R: 500, 502, 503, 504; Claude 529 | T |
| Claude usage probe | R | T | R\* | T | T |
| Claude refresh | R | T | T with guidance | T | T |
| Codex refresh | R | T | T with guidance | T | T |
| Claude heartbeat | R | T | R\* | T | T |
| Codex heartbeat | R | T | T with guidance | T | T |

The matrix has these exact interpretations:

- **Proven pre-send connect** is the narrow urllib3 connect category whose
  contract says the request was not sent. A `ProxyError` may be unwrapped only
  to that known category. TLS verification, unknown transport failures,
  `ProtocolError`, and broad `HTTPError` are not automatically classified as
  safe POST retries.
- **Ambiguous read/protocol** means request bytes might have reached the
  provider. Safe reads can repeat. Every current POST is terminal.
- **Claude Messages 429** is a documented rate-limit rejection with
  `retry-after`. The probe/heartbeat can retry only when the entire valid delay
  fits. The executor never truncates server guidance and retries early.
- **OAuth 429** remains terminal because generic API guidance is not
  endpoint-specific proof that a credential exchange was not applied. The
  resulting `RateLimitError` retains the last valid delay.
- **Codex heartbeat 429** remains terminal because the private backend is not
  covered by a documented idempotency/not-applied contract. Generic OpenAI
  rate-limit guidance does not change that.
- **Selected 5xx** for safe reads are 500, 502, 503, and 504, represented by
  stdlib `HTTPStatus` values. Claude's documented non-standard 529 overload
  status requires one named constant; it must not appear as a bare literal at
  provider call sites.
- No current POST retries a 5xx response. A future `x-should-retry: true`
  header is insufficient by itself unless the endpoint contract says it proves
  not-applied behavior or the request also uses a supported stable idempotency
  key. `x-should-retry: false` is always terminal.
- Provider-level access-token refresh after an application 401 is a separate
  workflow. It is not an HTTP transport retry with identical credentials.

## Candidate Comparison

| Criterion | urllib3 `Retry` owner | Retry-disabled urllib3 + Tenacity | Retry-disabled urllib3 + focused executor |
|---|---|---|---|
| Pooled sync transport | Yes | Yes | Yes |
| Connect/read distinction | Built in | Local predicate required | Local closed classifier required |
| Closed six-operation matrix | Possible through careful per-request objects | Local policy required | Direct immutable operation mapping |
| Terminal 429 response retained | Yes with `raise_on_status=False` | Yes with `retry_error_callback` | Yes as an ordinary terminal outcome |
| Integer and HTTP-date `Retry-After` | Built in | Local parser required | Local parser required |
| Guidance cap | Built in | Local | Local |
| Independent aware wall clock | No; direct `time.time()` | Local parser required | Yes |
| Independent monotonic sequence budget | No whole-operation budget | Custom local stop required; retry state still owns monotonic | Yes |
| Injected sleeper | No public injection; direct `time.sleep()` | Yes | Yes |
| Injected deterministic jitter | No; direct `random.random()` | Custom wait/RNG required | Yes |
| Typed Sidekick error preservation | Adapter required | Adapter and exhaustion bridge required | Adapter required |
| Contract-valid retry sketch | No; simple config fails time/deadline gate | Simple controller fails time/HTTP policy gate | 41 physical spike lines for loop/parser; 90-140 production-line projection |
| Runtime distributions | urllib3 | urllib3 and Tenacity | urllib3 |
| Retry-owner result | **NO-GO** | **NO-GO for current scope** | **Conditional GO** |

### urllib3 `Retry`

The official [`Retry` API](https://urllib3.readthedocs.io/en/2.7.0/reference/urllib3.util.html#urllib3.util.Retry)
supports total/connect/read/redirect/status/other counters, default idempotent
methods, status allowlists, response history, `Retry-After`, jitter, sensitive
header removal, and final-response return. Its `retry_after_max` cap correctly
applies to integer and HTTP-date guidance.

The blocker is deterministic ownership. Tagged 2.7.0 source uses:

- `random.random()` in `get_backoff_time`;
- `time.time()` in `parse_retry_after`; and
- `time.sleep()` in `sleep_for_retry` and `_sleep_backoff`.

See [tagged source lines 309-371](https://github.com/urllib3/urllib3/blob/2.7.0/src/urllib3/util/retry.py#L309-L371).
`Retry.new()` reconstructs `type(self)` from built-in constructor fields, so a
clock/RNG-carrying subclass must also override reconstruction and sleep
internals
([tagged source lines 266-282](https://github.com/urllib3/urllib3/blob/2.7.0/src/urllib3/util/retry.py#L266-L282)).
That is brittle integration with library internals. A per-attempt urllib3
`Timeout(total=...)` also is not a whole-retry-sequence deadline. An outer loop
that supervises urllib3's retry loop would create two owners.

### Tenacity

Tenacity's [`Retrying` API](https://tenacity.readthedocs.io/en/stable/api.html#tenacity.Retrying)
accepts explicit stop, wait, retry, sleep, and exhaustion callbacks. The spike
proved it can use a fake sleeper and return a terminal 429 result through
`retry_error_callback`.

Its tagged `RetryCallState` nevertheless creates and updates timestamps through
direct `time.monotonic()` calls
([Tenacity 9.1.4 source](https://github.com/jd/tenacity/blob/9.1.4/tenacity/__init__.py#L510-L570)).
A custom stop callback can consult Sidekick's clock, but then Sidekick owns the
actual elapsed contract while Tenacity's statistics use another clock.
Tenacity has no HTTP operation semantics, so the six-operation classifier,
transport exception subset, HTTP-date parser, guidance cap, final metadata,
and typed translations all remain local.

Bare `@retry` is prohibited. The official documentation states that its
default retries broad exceptions indefinitely without waiting
([Tenacity basic retry documentation](https://tenacity.readthedocs.io/en/stable/index.html#basic-retry)).

### Focused executor

The local option owns only the product semantics missing from the transport:

```text
RetryExecutor
├── closed RetryOperation -> immutable policy
├── total attempt count + monotonic elapsed budget
├── narrow transport/status outcome classification
├── Retry-After parser + aware UTC wall clock
├── bounded full-jitter delay + injected RNG
└── injected sleeper
```

Three current retry loops and six operation categories satisfy the rule of
three. The public `HttpClient` does not expose arbitrary policies, clock hooks,
provider callbacks, or `retry: bool`. Clock, sleeper, and RNG injection remain
private testable infrastructure inputs because the acceptance contract
specifically requires them.

The spike's minimal loop and parser were 41 physical lines including
signatures and docstrings. A production implementation needs closed policies,
urllib3 exception classification, terminal outcome data, and full jitter, so
90-140 cohesive lines is a projection rather than a promise. If the reviewed
module grows beyond roughly 150-200 substantive lines or recreates general
retry-library features, the decision must be reopened.

## Isolated Measurements

The experiments ran on Linux with CPython 3.14.6 in isolated environments.
Candidate versions were urllib3 2.7.0, Tenacity 9.1.4, HTTPX 0.28.1, and
Stamina 26.1.0. They used a local HTTP/1.1 server and synthetic bodies; no
provider account, token, quota, or remote endpoint was used.

### Request shapes and pooling

| Probe | Exact result |
|---|---|
| GET JSON | Decoded `{"ok": true}` |
| POST JSON | JSON object round-tripped |
| POST form | `grant_type=refresh_token` and `scope=read` round-tripped |
| POST bytes to headers | Retained response header `X-Usage-Limit: 42` while discarding the body |
| Pool reuse | Six observed requests used one unique client source port |

The one-port result demonstrates local connection reuse. It does not prove
remote TLS session reuse, proxy tunneling, or provider latency.

### Retry-owner behavior

| Candidate/probe | Exact result |
|---|---|
| urllib3 final 429 | Two attempts; terminal status 429; final `Retry-After: 0`; one retry-history entry |
| urllib3 integer guidance cap | Input 99 seconds became the configured two-second cap |
| urllib3 HTTP-date cap | Date 300 seconds ahead became the configured two-second cap |
| urllib3 time inspection | Direct wall-time, sleep, and random calls were all present |
| Tenacity final 429 | Final result remained 429; one fake zero-second sleep occurred |
| Tenacity time inspection | Direct `time.monotonic()` was present in retry state |
| Focused attempt/deadline | Terminal 503 after attempt two of a five-attempt budget; one synthetic one-second sleep; the next two-second delay would cross a 2.5-second deadline |
| Focused guidance parser | Integer 999 and date 300 seconds ahead capped to 120; a past date became zero; malformed text returned no guidance |

### Installed size and fresh-process proxy

Thirty fresh processes were measured in separate minimal CPython 3.14.6
environments. These figures are startup proxies, not end-to-end CLI results.

| Environment/import | Candidate installed bytes | Median | p95 |
|---|---:|---:|---:|
| Python baseline (`import sys`) | 0 | 11.66 ms | 12.61 ms |
| urllib3 | 432,560 | 78.73 ms | 105.71 ms |
| urllib3 + Tenacity | 519,092 | 84.79 ms | 105.29 ms |
| HTTPX control closure | 1,766,393 | 74.44 ms | 98.55 ms |
| urllib3 + Stamina control | 568,578 | 74.82 ms | 118.76 ms |

The counterintuitive control medians demonstrate scheduler/filesystem noise;
they do not make HTTPX a smaller integration. All medians are minor beside
network latency. The later implementation must measure actual root help,
version, one-account, and representative multi-account paths.

### Homebrew closure

The repository formula generator resolves every runtime distribution with
`uv pip compile` and emits one resource per package, preferring sdists.
Isolated compiles produced:

| Candidate | Total formula resources | Increase over current eight |
|---|---:|---:|
| urllib3 + focused executor | 9 | 1 (`urllib3`) |
| urllib3 + Tenacity | 10 | 2 (`urllib3`, `tenacity`) |
| HTTPX control | 14 | 6 (`httpx`, `httpcore`, `h11`, `certifi`, `idna`, `anyio`) |

## Retry-After and Deadline Contract

[RFC 9110 section 10.2.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-10.2.3)
defines `Retry-After` as an HTTP date or a non-negative integer number of
seconds. The selected executor must:

1. accept ASCII decimal delay seconds and reject negative values;
2. otherwise parse a standards-compliant HTTP date with
   `email.utils.parsedate_to_datetime`;
3. compare dates to an independently injected aware UTC wall clock;
4. use an independently injected monotonic clock for the whole-operation
   budget;
5. convert past dates to zero;
6. treat malformed guidance as absent and use bounded full jitter;
7. cap valid guidance at 21,600 seconds initially, matching urllib3 2.7.0's
   defensive six-hour default;
8. retain the last valid capped value across attempts, so malformed later
   guidance does not erase useful terminal metadata;
9. terminate without sleeping or retrying when the complete server delay does
   not fit the remaining deadline; and
10. pass the retained value to terminal `RateLimitError` for user guidance.

The first implementation should use explicit names and distinguish attempts
from retries. Three total attempts, a 15-second whole-operation budget, a
three-second connect timeout, and a ten-second read timeout are the initial
proposal. Before each attempt, clamp the transport's per-attempt total timeout
to the remaining monotonic budget. These values remain subject to real CLI
timing evidence during CS-11; the approved requirement for both attempt and
elapsed bounds does not.

Full jitter selects uniformly between zero and the capped exponential delay.
The RNG is injected for deterministic tests. It need not be cryptographic
because the value controls scheduling, not credential material.

## Pooling TLS CA Proxy and Redirect Boundary

### Pool and lifecycle

The [urllib3 user guide](https://urllib3.readthedocs.io/en/2.7.0/user-guide.html#making-requests)
recommends an application-created `PoolManager` instead of the module-global
helper. One invocation-scoped `HttpClient` owns its manager(s) and calls
`clear()` through the Click/Typer close boundary. Help and version do not
construct the client. Tests use a fake transport or constructed client instead
of patching a library-global request function.

### HTTPS TLS and CA

The client rejects a non-HTTPS or otherwise forbidden scheme before pool
access. urllib3 verifies HTTPS certificates by default and attempts to load
default system certificate stores
([certificate verification guide](https://urllib3.readthedocs.io/en/2.7.0/user-guide.html#certificate-verification)).

The implementation must preserve system CA discovery and standard
`SSL_CERT_FILE`/`SSL_CERT_DIR` behavior rather than hard-code a Linux bundle.
Adding `certifi` is not justified merely because HTTPX would bring it. Invalid
certificates, hostname failures, or TLS handshake/configuration errors are
terminal typed application failures; the client never disables verification
or falls back to plaintext.

Separate connect and read timeouts are mandatory. Response and error bodies
are read with explicit maximum sizes, even when urllib3 would preload them.
Request bodies also have operation-appropriate bounds.

### Environment proxy

The current `urllib.request` default opener discovers environment proxies
([Python 3.14 `getproxies`](https://docs.python.org/3.14/library/urllib.request.html#urllib.request.getproxies)).
urllib3 requires explicit `ProxyManager` construction
([urllib3 `ProxyManager`](https://urllib3.readthedocs.io/en/2.7.0/reference/urllib3.poolmanager.html#urllib3.ProxyManager)).
Changing transports must not silently remove current proxy behavior.

The client factory therefore reuses stdlib proxy discovery and bypass
semantics rather than inventing an environment parser. It must:

- honor the applicable HTTPS proxy and `NO_PROXY`/proxy-bypass result;
- use one direct pool and the selected explicit proxy manager as private
  implementation details;
- support only proxy schemes urllib3 supports without an unapproved extra;
- reject unsupported schemes as a typed configuration failure;
- keep proxy credentials out of exceptions, observations, and output; and
- avoid adding SOCKS support/PySocks until a concrete caller requires it.

### Redirects

Provider and OAuth endpoint identities are fixed and carry bearer credentials.
No current requirement needs redirect following. Pass `redirect=False` on
every request and treat 3xx as terminal without forwarding credentials.

This is defense in depth even on patched urllib3 2.7.0.
[GHSA-pq67-6m6q-mj2v](https://github.com/urllib3/urllib3/security/advisories/GHSA-pq67-6m6q-mj2v)
describes a pre-2.5 bug in which manager-level retry configuration did not
disable redirects as expected and identifies request-level `redirect=False`
as remediation. The selected version is outside the affected range; the
request-level rule makes the security intent explicit and resilient.

## Typed Error and Security Boundary

Transport responses are internal outcomes. Successful public calls return
only a runtime-validated JSON object or normalized headers. Failures raise
Sidekick application errors:

| Terminal condition | Public Sidekick outcome |
|---|---|
| HTTP 401 | Existing `AuthError`; never replay with identical credentials |
| HTTP 403 | Existing `ForbiddenError`; preserve bounded safe diagnostic fields |
| Terminal/exhausted 429 | Existing `RateLimitError` with last valid bounded guidance |
| Exhausted eligible connect/read/5xx | Existing `TransientError` |
| Ambiguous POST read/protocol failure | Immediate `TransientError` stating the request was not replayed |
| Non-HTTPS or forbidden scheme | Design-approved `InsecureUrlError` before transport |
| Malformed, oversized, or non-object JSON | One design-approved typed invalid-payload error |
| TLS/proxy configuration failure | Typed terminal application error with secrets and URL query redacted |
| Permanent non-auth HTTP status | Terminal non-transient application failure; never falsely labeled transient |

Before adding a new error name, implementation must search `errors.py` for the
exact concept and reuse the existing vocabulary where truthful. It must not
create a transport-library error hierarchy. No urllib3, Tenacity, HTTPX,
Stamina, or stdlib transport exception crosses `http/`.

The security boundary prohibits all of these from error strings, structured
details, observations, logs, JSON, quiet, scheduled, or normal output:

- access, refresh, and ID tokens;
- authorization and proxy-authorization headers;
- request credential payloads;
- proxy passwords and URL query strings;
- full provider/account identifiers; and
- unbounded or unredacted provider error bodies.

Retry observation is not implemented until a concrete consumer exists. If one
is later justified, it can contain only the non-sensitive operation/category,
attempt, elapsed budget, status/failure category, selected delay/source, and
retrying/terminal state.

## Packaging Platform License and Maintenance

### urllib3

[urllib3 2.7.0 on PyPI](https://pypi.org/project/urllib3/) records:

- release date 2026-05-07;
- Python `>=3.10` and a Python 3.14 classifier;
- OS-independent, universal `py3-none-any` packaging;
- no mandatory dependencies;
- a 131.1 kB wheel and 433.6 kB sdist; and
- MIT licensing.

The 2.7.0 GitHub release is signed and reports fixes for high-severity
streaming decompression issues and a cross-host `ProxyManager`
sensitive-header stripping issue
([urllib3 2.7.0 release](https://github.com/urllib3/urllib3/releases/tag/2.7.0)).
The same release requests funding after a sharp decline in financial support.
That is a maintenance sustainability risk to monitor, not evidence that the
current package is abandoned.

### Tenacity

[Tenacity 9.1.4 on PyPI](https://pypi.org/project/tenacity/) records:

- release date 2026-02-07;
- Python `>=3.10` and a Python 3.14 classifier;
- universal pure-Python packaging;
- no mandatory dependencies;
- a 28.9 kB wheel and 49.4 kB sdist; and
- Apache-2.0 licensing.

The package uses trusted publishing/provenance. GitHub currently reports no
published advisories but also no detected `SECURITY.md`
([Tenacity security overview](https://github.com/jd/tenacity/security)). The
absence of a published advisory is not proof that vulnerabilities do not
exist. This governance limitation reinforces, but does not independently
decide, the no-add recommendation.

### Controls and platforms

[HTTPX 0.28.1](https://pypi.org/project/httpx/) is a capable sync/async client,
but the isolated closure added six distributions and capabilities no current
Sidekick operation needs. [Stamina 26.1.0](https://pypi.org/project/stamina/)
is an active Tenacity wrapper but adds another layer and observation surface
without supplying HTTP operation semantics or independent clocks.

urllib3 and Tenacity's universal wheels remove native wheel splits, and PyPI
declares Python 3.14 support. Only Linux CPython 3.14.6 executed the spike.
Metadata is strong installation evidence, not proof of Windows certificate
stores, macOS CA behavior, WSL proxy inheritance, or Homebrew operation. The
existing native CI/release matrix remains a conditional gate.

All selected/reviewed licenses are compatible with Sidekick's Apache-2.0
license. Advisory, release, provenance, and maintainer facts are time-bound and
must be refreshed at the actual pin or upgrade.

## Approved Decision

### Approved disposition

The operator recorded **GO** on 2026-07-10 for:

1. urllib3 2.7.0 as the pooled sync transport; and
2. one focused local executor over retry-disabled urllib3 as the sole retry
   owner.

The decision records **NO-GO for the current scope** for:

- urllib3 `Retry` as owner;
- Tenacity as owner;
- HTTPX merely for hypothetical async/HTTP2 support; and
- Stamina as an additional wrapper.

### Required implementation conditions

The approval becomes releasable production authority only after CS-11 proves:

- exactly one retry owner and no provider-level/manual/urllib3 stacking;
- the complete six-operation matrix;
- terminal typed errors and retained final rate-limit guidance;
- both standard `Retry-After` forms, cap, last-valid retention, and monotonic
  elapsed stop;
- one invocation-scoped pool and deterministic close;
- no client construction on help/version;
- HTTPS-only verified TLS, system/custom CA behavior, and bounded bodies;
- preserved environment HTTPS proxy and `NO_PROXY` behavior;
- request-level retry and redirect disablement;
- redaction of every credential, proxy secret, identifier, and body;
- Linux, macOS, Windows, WSL, wheel, lockfile, and Homebrew acceptance; and
- unchanged human, JSON, quiet, scheduled, and version output.

The operator approval authorizes the selected dependency and implementation;
it does not waive or defer any condition above.

## Implementation Implications

The approved target package remains:

```text
http/
├── __init__.py
├── client.py
└── retry.py
```

`http/client.py` owns:

- the direct pool/proxy manager lifecycle;
- HTTPS/TLS/CA and environment proxy selection;
- four concrete request encodings;
- `HTTPMethod` request construction;
- bounded request/response and error-body reads;
- JSON-object validation and normalized headers;
- request-level `retries=False` and `redirect=False`; and
- terminal Sidekick error translation.

`http/retry.py` owns:

- the closed six-operation vocabulary and immutable policies;
- attempt and monotonic elapsed bounds;
- narrow transport/status eligibility;
- standards-compliant `Retry-After` with aware wall time;
- bounded full jitter; and
- one terminal outcome.

The package imports no provider, CLI, Rich, Typer, persistence, account, or
renderer module. Providers import only the public `HttpClient` façade and
select a closed operation. Core never imports HTTP infrastructure.

Before implementing, search the package for the exact concepts
`Retry-After`, `retry`, `deadline`, `RateLimitError`, `TransientError`,
`ForbiddenError`, proxy, CA, redirect, and timeout. Replace the existing
helpers and all three loops atomically. Do not leave a compatibility loop, a
second policy map, or a transport protocol with one implementation.

The design does not authorize a circuit breaker, arbitrary observer hook,
request-class hierarchy, generic resilience service, async client, HTTP/2,
provider-created retry configuration, or speculative policy parameters.

## Limitations and Open Risks

- The request/pool spike used loopback HTTP. It did not prove TLS session reuse,
  real proxy tunnels, certificate failures, or provider behavior.
- Only Linux CPython 3.14.6 executed the code. Universal wheel metadata and
  classifiers do not replace native Windows/macOS/WSL validation.
- No endpoint-specific idempotency/not-applied documentation was found for
  either OAuth refresh endpoint or the private Codex heartbeat endpoint. Their
  conservative terminal policies are intentional.
- The Claude unified inference-header schema is effectively provider-private.
  Retry safety does not stabilize its response contract.
- The 15-second elapsed budget and connect/read split are initial proposals,
  not measured latency objectives.
- urllib3 does not automatically preserve the current default opener's
  environment proxy behavior. The explicit factory and native tests are
  mandatory.
- Installed size and process timing are one-host measurements affected by
  filesystem cache and scheduling. They establish scale, not a millisecond
  ranking.
- The local executor's production line count is projected. Its actual reviewed
  shape controls whether the buy-versus-build conclusion remains valid.
- Current security/advisory findings are point-in-time observations. A safe
  floor and ongoing dependency updates are required.
- The formula resource comparison resolves the current candidate closures but
  does not replace a clean formula installation on supported Homebrew hosts.

## Reversal and Stop Conditions

Reopen the retry-owner decision if:

- urllib3 exposes supported independent wall-clock, monotonic deadline,
  sleeper, and RNG injection without subclassing/private hooks;
- the focused executor exceeds roughly 150-200 substantive lines, recreates a
  generic retry engine, or serves additional materially different retry
  domains;
- concrete cancellation, async, streaming, HTTP/2, or consumed observation
  requirements arise;
- a provider adds a documented idempotency key or authoritative not-applied
  signal that changes a POST matrix row; or
- a selected library's security, license, provenance, release, or maintenance
  posture materially changes.

Reopen the transport decision instead of silently retaining or selecting a
non-pooled fallback if:

- urllib3 cannot preserve environment proxy/CA behavior;
- verified HTTPS, redirects-off behavior, bounded reads, or typed error
  translation fails;
- Linux, macOS, Windows, WSL, wheel, or Homebrew acceptance fails; or
- package maintenance/security posture becomes unacceptable.

Stop CS-11 immediately if implementation creates two retry owners, permits an
ambiguous POST replay outside the matrix, loses terminal 429 guidance, leaks a
transport exception/credential, follows a credential-bearing redirect, or
silently weakens proxy/TLS behavior.

## Test and Verification Contract

No production tests are warranted for this research-only documentation record:
it changes no executable code, dependency, configuration, or product behavior.
The appropriate verification for this change is Markdown lint, direct-link
validation, an unresolved-marker/secret scan, and confirmation that the
requested document is the only tracked change.

The later CS-11 implementation must add the fewest concise, load-bearing
behavior tests:

1. one local-server test for all four shapes, pool reuse, bounded reads, and
   deterministic closure;
2. one parameterized six-operation failure/retry matrix;
3. one table-driven integer/date/past/malformed/capped `Retry-After` contract;
4. one injected aware-wall/monotonic/sleeper/RNG deadline test proving elapsed
   exhaustion while attempts remain;
5. one typed-boundary table covering scheme, 401, 403, 429, transport
   exhaustion, malformed/non-object/oversized JSON, and permanent status;
6. one credential/proxy secret-sentinel test across errors and observations;
7. one proxy/`NO_PROXY`/redirect test proving authorization is not forwarded;
   and
8. one composition/output test proving lazy initialization, closure, and
   unchanged human/JSON/quiet/scheduled/version behavior.

Tests assert Sidekick behavior and stable application errors, not library
internals, complete rendered snapshots, or a coverage number. Related cases
should be parameterized when that keeps the distinct failure reason obvious;
copy-paste coverage padding is not acceptable.

## Primary Source List

All web sources were accessed on 2026-07-10.

### Standards and provider guidance

- [RFC 9110: HTTP Semantics](https://www.rfc-editor.org/rfc/rfc9110.html)
- [RFC 9700: OAuth 2.0 Security Best Current Practice](https://www.rfc-editor.org/rfc/rfc9700.html)
- [Anthropic API errors](https://platform.claude.com/docs/en/api/errors)
- [Anthropic rate limits](https://platform.claude.com/docs/en/api/rate-limits)
- [OpenAI 429 guidance](https://help.openai.com/en/articles/5955604-how-can-i-solve-429-too-many-requests-errors)

### urllib3

- [urllib3 2.7.0 user guide](https://urllib3.readthedocs.io/en/2.7.0/user-guide.html)
- [urllib3 2.7.0 `Retry` API](https://urllib3.readthedocs.io/en/2.7.0/reference/urllib3.util.html#urllib3.util.Retry)
- [urllib3 2.7.0 pool manager API](https://urllib3.readthedocs.io/en/2.7.0/reference/urllib3.poolmanager.html)
- [urllib3 2.7.0 tagged retry source](https://github.com/urllib3/urllib3/blob/2.7.0/src/urllib3/util/retry.py)
- [urllib3 2.7.0 release](https://github.com/urllib3/urllib3/releases/tag/2.7.0)
- [urllib3 PyPI metadata](https://pypi.org/project/urllib3/)
- [urllib3 redirect advisory GHSA-pq67-6m6q-mj2v](https://github.com/urllib3/urllib3/security/advisories/GHSA-pq67-6m6q-mj2v)

### Tenacity and controls

- [Tenacity stable documentation](https://tenacity.readthedocs.io/en/stable/)
- [Tenacity API](https://tenacity.readthedocs.io/en/stable/api.html)
- [Tenacity 9.1.4 tagged source](https://github.com/jd/tenacity/blob/9.1.4/tenacity/__init__.py)
- [Tenacity 9.1.4 PyPI metadata](https://pypi.org/project/tenacity/)
- [Tenacity security overview](https://github.com/jd/tenacity/security)
- [HTTPX PyPI metadata](https://pypi.org/project/httpx/)
- [Stamina PyPI metadata](https://pypi.org/project/stamina/)

### Platform and production examples

- [Python 3.14 environment proxy discovery](https://docs.python.org/3.14/library/urllib.request.html#urllib.request.getproxies)
- [pip pooled retry session](https://github.com/pypa/pip/blob/main/src/pip/_internal/network/session.py)
- [OpenAI Python base client](https://github.com/openai/openai-python/blob/main/src/openai/_base_client.py)
- [Stripe Python HTTP client](https://github.com/stripe/stripe-python/blob/master/stripe/_http_client.py)
- [Stripe Python request/idempotency handling](https://github.com/stripe/stripe-python/blob/master/stripe/_api_requestor.py)

### Repository evidence

- `src/sidekick_usages/http.py`
- `src/sidekick_usages/errors.py`
- `src/sidekick_usages/update.py`
- `src/sidekick_usages/providers/claude.py`
- `src/sidekick_usages/providers/codex.py`
- `src/sidekick_usages/heartbeat/claude.py`
- `src/sidekick_usages/heartbeat/codex.py`
- `tests/test_http_errors.py`
- `packaging/homebrew/generate.py`
- `.github/workflows/ci.yml`
- `pyproject.toml`

# HTTP transport and retry behavior

Sidekick has one invocation-scoped HTTP client shared by provider adapters and
application services. This guide documents its security, timeout, pooling,
proxy, retry, and error contracts.

## Connection and TLS policy

- Every application request must use an absolute `https://` URL with a host.
  User information, invalid ports, and non-HTTPS schemes are rejected before
  transport access.
- Certificate verification is required for direct and proxy connection pools.
- Redirect following is disabled. A provider cannot silently redirect OAuth
  credentials to another host.
- Direct and proxy connections are pooled for one composed CLI invocation and
  closed exactly once when that invocation exits.
- Successful bounded reads release reusable connections back to the pool.
  Oversized, malformed, or failed reads close the affected response safely.

Sidekick uses the operating system and Python proxy environment discovered by
`urllib.request.getproxies()`, including `HTTPS_PROXY` and `NO_PROXY` behavior.
Proxy credentials and URLs are never included in translated application error
messages.

## Bounds

Each operation has:

- a 3-second connection timeout;
- a 10-second read timeout;
- a 15-second total monotonic operation budget; and
- at most 3 attempts, including the first attempt.

Every request and response shape has a fixed byte limit. JSON responses are
limited to 4 MiB, JSON requests to 1 MiB, form requests to 256 KiB, and
discarded or error bodies to 64 KiB. JSON must decode to an object, not an
arbitrary scalar or list.

## Closed retry policy

Retry safety is selected from a closed operation enum. Provider adapters
cannot pass an arbitrary retry flag or a transport-library retry object.

| Operation | Proven connection failure | Ambiguous transport failure | HTTP 429 | Selected 5xx |
| --- | --- | --- | --- | --- |
| Safe GET | Retry | Retry | Retry | Retry |
| Claude usage probe | Retry | Stop | Retry | Stop |
| Claude refresh | Retry | Stop | Stop | Stop |
| Codex refresh | Retry | Stop | Stop | Stop |
| Claude heartbeat | Retry | Stop | Retry | Stop |
| Codex heartbeat | Retry | Stop | Stop | Stop |

A proven connection failure occurred before a request could be sent. An
ambiguous transport failure may have happened after transmission, so only a
safe read can repeat it. Mutating OAuth and model-request operations do not
retry ambiguous failures.

Eligible retries use full-jitter exponential backoff. A valid RFC 9110
`Retry-After` delay takes precedence, but a delay that does not fit inside the
15-second operation budget is returned as operator guidance rather than slept.
Parsed guidance is capped at six hours. `X-Should-Retry: false` makes a status
terminal even when its class would otherwise be retryable.

## Error behavior

The transport translates library and protocol failures into Sidekick-owned
errors. Callers can distinguish authentication, forbidden scope, rate limit,
transient transport or server failure, invalid payload, insecure URL, and
other terminal HTTP status outcomes without importing `urllib3` types.

Error bodies are bounded before parsing. Provider tokens, proxy credentials,
rejected JSON input, raw validation errors, and arbitrary response bodies are
not chained, logged, or rendered. A 429 preserves a safe whole-second
`retry_after` value when the provider supplied one.

## Troubleshooting

For a transient or rate-limit failure:

1. Run the command once more after the displayed `Retry-After` interval.
2. Verify the system clock if an HTTP-date delay appears incorrect.
3. Check `HTTPS_PROXY` and `NO_PROXY` without copying credentials into logs.
4. Confirm the system trust store can validate the provider host.
5. Run `sidekick-usages doctor` to separate credential failure from network
   failure.

Sidekick does not provide an insecure TLS mode, arbitrary retry hooks, or a
command-line timeout override. Those would weaken the reviewed operation
contract.

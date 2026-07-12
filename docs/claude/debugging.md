# Claude account debugging

- **Status:** Active symptom-first operational guidance
- **Source and command verification:** 2026-07-12
- **Claude Code release:** `2.1.207`
- **Sidekick evidence commit:**
  `15cef27bf91029f911d87597efca9e410b3a67fd`

A running log of non-obvious debugging techniques and root causes
encountered with `sidekick-usages`'s Claude provider. Add new entries
to the [Index](#index) as they come up, and follow the
[conventions](#conventions-for-adding-entries) below so each entry
stays self-contained and skimmable.

## Index

- [HTTP 401 in `check` after a token refresh](#http-401-in-check-after-a-token-refresh)
  — token bytes are valid against `/v1/messages` directly, but
  `sidekick-usages` still reports 401. Covers the direct-probe
  technique, response-header decoding
  (`anthropic-organization-id`, `overage-disabled-reason`), and the
  two false leads (cosmetic plan tag, stale `account.scopes`).
- [Claude refresh token fails with HTTP 400, 403, or 429](#claude-refresh-token-fails-with-http-400-403-or-429)
  — saved OAuth access token is expired, but direct refresh does not
  behave like the installed Claude Code binary. Covers the
  `platform.claude.com` token endpoint, the Claude Code client id,
  and the isolated-`HOME` CLI refresh path.
- *Add new entries here as they come up.*

## Conventions for adding entries

Each entry is an H2 section answering one question: "what does this
symptom mean and how do I get unstuck?" Keep them in the shape:

1. **Symptom** — the exact terminal output or behavior. Copy-paste,
   don't paraphrase. Future-you greps this section.
2. **Don't be fooled** — false leads that look like the cause but
   aren't. Link the owning source file and name the relevant symbol;
   avoid brittle raw line-number citations.
3. **Diagnostic** — a probe that isolates the symptom from
   `sidekick-usages`'s code path (usually a direct curl). Includes
   how to read the output.
4. **Root causes** — the real explanations, ranked by frequency.
5. **Fix** — the specific commands to run, with redacted secrets.

Redact tokens (`sk-ant-oat01-<REDACTED>`) and anonymize identifiers
(`<ORG_UUID_A>`) when including worked examples. The technique is
the reusable content; the secret it was applied to is not.

Never put a real token in shell history, command arguments, documentation, or
diagnostic output. Use a hidden prompt and pass the value through a bounded
stdin or child-environment boundary only for the lifetime of the probe. Unset
the shell variable immediately afterward.

Add a one-line summary to the [Index](#index) above with an anchor
link to the new section.

---

## Claude refresh token fails with HTTP 400, 403, or 429

A saved Claude login account has both `access_token` and
`refresh_token`, but `sidekick-usages check` cannot renew it.

### Symptom

Any of these errors during the refresh step:

```text
Token refresh failed: HTTP 400: Bad Request
Token refresh failed: Rate limited (HTTP 429) after 3 attempts.
Token refresh failed: HTTP 403 Forbidden (no body).
Token refresh failed: Claude CLI refresh failed: Login failed: Request failed with status code 400
```

### Don't be fooled

The `sk-ant-ort01-...` refresh token can be real and still fail if
the refresh request does not match Claude Code's own OAuth client
flow. The current Sidekick boundary and installed Claude Code 2.1.207 use:

- token endpoint: `https://platform.claude.com/v1/oauth/token`
- client id: `9d1c250a-e61b-44d9-88ed-5944d1962f5e`
- JSON body fields: `grant_type`, `refresh_token`, `client_id`,
  `scope`, and sometimes `expires_in`

Older direct-refresh code used `https://api.anthropic.com/v1/oauth/token`
with the metadata-document client id
`https://claude.ai/oauth/claude-code-client-metadata`. That request
shape is not equivalent to the installed CLI.

During the original investigation, Python `urllib` requests hit edge behavior
that the Claude binary did not: dummy token probes returned Cloudflare 1010
without the Claude Code user agent and Anthropic `rate_limit_error` 429 with
it. The installed `claude auth login --claudeai` path succeeded with the same
saved refresh token in an isolated temporary `HOME`. These response details are
historical observations; the supported current behavior is the isolated CLI
path followed by the bounded platform fallback described below.

### Diagnostic

Use the installed Claude binary itself, but isolate it from your real
`~/.claude` login:

```bash
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

IFS= read -r -s -p "Claude refresh token: " claude_refresh_token
printf '\n'

CLAUDE_CODE_OAUTH_REFRESH_TOKEN="$claude_refresh_token" \
CLAUDE_CODE_OAUTH_SCOPES='user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload' \
HOME="$tmp" \
claude auth login --claudeai
unset claude_refresh_token

jq '{expiresAt: .claudeAiOauth.expiresAt,
     scopes: .claudeAiOauth.scopes,
     subscriptionType: .claudeAiOauth.subscriptionType}' \
  "$tmp/.claude/.credentials.json"
```

- **`Login successful.` and a temp credentials file exists** -> the
  refresh token is valid; `sidekick-usages` must import the rotated
  temp credentials and save them.
- **`Login failed: Request failed with status code 400`** -> Claude
  Code itself rejected the refresh token. Treat it as expired,
  revoked, or bound to a login you no longer have.

### Refresh root causes

1. Direct refresh request shape drifted from Claude Code's current
   OAuth client metadata.
2. The platform token endpoint behaves differently for the official
   Claude binary than for Python `urllib`, even with similar visible
   JSON fields.
3. Some saved refresh tokens are genuinely dead. In that case the
   installed Claude binary rejects them too, usually with status 400.

### Fix

On non-macOS systems with Claude Code installed, `sidekick-usages` first
tries to refresh saved Claude OAuth accounts by running:

```text
claude auth login --claudeai
```

inside an isolated temporary home with
`CLAUDE_CODE_OAUTH_REFRESH_TOKEN` and `CLAUDE_CODE_OAUTH_SCOPES` set
from the saved account. It then parses the temporary
`.claude/.credentials.json`, imports the rotated access/refresh
tokens into the account store selected by `doctor`, and removes the temporary
home.

On macOS, or when no Claude executable can be resolved, Sidekick uses its
bounded direct HTTPS OAuth exchange. Both paths leave the active `~/.claude`
login untouched. An explicit CLI rejection remains terminal and is not masked
by the HTTPS fallback.

If sidekick reports `Claude CLI refresh failed`, the saved refresh
token is dead according to Claude Code itself. Log into the matching
Claude account normally, then run:

```bash
sidekick-usages refresh "your-label"
```

Do not blindly refresh a different saved Claude label while logged
into the wrong Claude account; that overwrites the label with the
currently active local Claude login.

---

## HTTP 401 in `check` after a token refresh

You just rotated a Claude OAuth token (via `claude setup-token` or
`claude auth login` plus `sidekick-usages refresh <label>`), and
`sidekick-usages check` still reports 401 for one or more accounts.
This entry walks through verifying the token bytes independently of
this tool, decoding what the response headers say about *which*
account a token belongs to, and the two false leads that look like
causes but aren't.

### Symptom

```
$ sidekick-usages --only claude check
you@example-org@org  [claude · team]
  HTTP 401: token expired or invalid
```

### Don't be fooled

Two things look like the cause but aren't:

#### 1. The plan tag in the rendered output is cosmetic

`sidekick-usages` shows accounts as `[claude · team]`,
`[claude · max]`, `[claude · pro]`. That string comes from
`account.plan`, which is parsed from the local Claude CLI credential boundary
(`subscriptionType` in
[`providers/claude/schemas.py`](../../src/sidekick_usages/providers/claude/schemas.py))
and used only for color-coding in
[`usage/narrow_render.py`](../../src/sidekick_usages/usage/narrow_render.py)
and the wide usage renderer.

It is **not** consulted during auth. The `fetch_usage` dispatch in
[`providers/claude/usage.py`](../../src/sidekick_usages/providers/claude/usage.py)
routes on saved scopes, not on plan:

```python
credentials = require_claude_credentials(account)
if credentials.scopes is not None and PROFILE_SCOPE not in credentials.scopes:
    return fetch_via_headers(account, http)  # /v1/messages probe
return fetch_via_oauth_endpoint(account, http)  # /api/oauth/usage
```

Both code paths send the **same token bytes** as
`Authorization: Bearer …`. There is no `--plan max` flag that changes
the request. If the plan label is wrong on a saved account, that's a
display bug — it cannot cause a 401.

#### 2. "Token expired" isn't always token expiry

Anthropic's API returns 401 for any malformed `Authorization` header,
not just rotated/revoked tokens. The two most common non-expiry
causes (see [401 root causes](#401-root-causes) below):

- whitespace in the stored token bytes (leading space, trailing `\n`,
  shell-quoting accidents);
- a stale `scopes` field on the saved account that routes the request
  down the wrong code path.

### Diagnostic: probe the token directly

The first thing to verify is whether the token itself works against
Anthropic's API, independent of anything `sidekick-usages` stored or
sent. `/v1/messages` is the same endpoint the `fetch_via_headers`
path in
[`providers/claude/usage.py`](../../src/sidekick_usages/providers/claude/usage.py),
so a direct curl bypasses every layer of this tool.

The following function reads the token without echo, validates the same token
grammar Sidekick accepts, and sends curl configuration through stdin. The token
does not appear in shell history, curl arguments, or curl's environment:

```bash
probe_claude_token() {
  local claude_token claude_version
  IFS= read -r -s -p "Claude access token: " claude_token
  printf '\n'

  if [[ ! "$claude_token" =~ ^sk-ant-oat01-[A-Za-z0-9_-]+$ ]]; then
    printf 'Token does not match the Claude OAuth token grammar.\n' >&2
    return 2
  fi

  claude_version="$(claude --version | awk '{print $1}')"
  {
    printf 'url = "https://api.anthropic.com/v1/messages"\n'
    printf 'request = "POST"\n'
    printf 'header = "Authorization: Bearer %s"\n' "$claude_token"
    printf 'header = "anthropic-version: 2023-06-01"\n'
    printf 'header = "anthropic-beta: oauth-2025-04-20"\n'
    printf 'header = "User-Agent: claude-code/%s"\n' "$claude_version"
    printf 'header = "Content-Type: application/json"\n'
    printf '%s\n' \
      'data = "{\"model\":\"claude-haiku-4-5-20251001\",\"max_tokens\":1,\"messages\":[{\"role\":\"user\",\"content\":\"q\"}]}"'
  } | curl --silent --include --config - \
    | grep -iE \
      '(^HTTP/|anthropic-ratelimit-|anthropic-organization-id|error)'
}

probe_claude_token
unset -f probe_claude_token
```

This is still a real one-word model request and consumes a small amount of
quota. Do not run it on a shared or hostile host where another process owned by
the same user can inspect runtime memory or pipes.

- **HTTP 200** → the token is valid; the 401 is something inside
  `sidekick-usages` (whitespace, stale scopes, wrong account
  selected). Jump to [401 root causes](#401-root-causes).
- **HTTP 401** → the token is genuinely revoked/expired/wrong. Mint
  a new one (`claude setup-token` or `claude auth login`).
- **HTTP 403** → the provider recognized the request but denied the Messages
  operation because of scope, account policy, or another authorization rule.

### What the response headers reveal

The subscription-specific unified headers below are private observations, not
published stable Anthropic contracts. Sidekick parses only the narrow 5-hour
and 7-day utilization/reset fields it needs. Use the other values as
corroborating troubleshooting evidence only; never use them as authorization,
durable identity, or definitive plan classification.

| Observed header | Safe interpretation |
|---|---|
| `anthropic-organization-id` | Correlates responses within one dated investigation; it is not a Sidekick account-identity contract. |
| `anthropic-ratelimit-unified-overage-status` | Reports the observed overage state; it does not prove a subscription plan. |
| `anthropic-ratelimit-unified-{5h,7d}-utilization` | Supplies the utilization values Sidekick validates and renders. |
| `anthropic-ratelimit-unified-{5h,7d}-reset` | Supplies the reset instants Sidekick validates and renders. |

#### Dated overage observations

During the original investigation, a rejected overage response also included
an `overage-disabled-reason`:

- `group_zero_credit_limit` appeared on an organization-managed workspace where
  an administrator had disabled overage spending.
- `usage_limit_reached` appeared when the observed account had exhausted its
  usage allowance.

Those strings are useful forensic context for that capture. Anthropic does not
currently publish them as an exhaustive or stable plan taxonomy, so future
responses may add values or change their meaning.

#### Worked example

Two tokens probed at the same time both returned 200:

| Label | Observed organization id | Overage | Reason |
|---|---|---|---|
| Work label | `<ORG_UUID_A>` | `rejected` | `group_zero_credit_limit` |
| Personal label | `<ORG_UUID_B>` | `allowed` | — |

The different observed organization ids, overage states, and utilization values
corroborated the expected labels in that one investigation. They did not create
a durable provider identity mapping. If a saved label is suspect, reauthenticate
the intended account and let Sidekick's supported import and identity checks
replace or reject it.

### 401 root causes

When the direct curl returns 200 but `sidekick-usages` still 401s,
one of these is almost always the cause.

#### Whitespace in stored token bytes

The most common path to a "phantom" 401: the saved `access_token`
contains a leading space, trailing newline, or shell-quoting artifact.
Anthropic rejects `Authorization: Bearer  sk-ant-…` (double space)
with 401.

The classic source is `export X= sk-ant-…` — note the space after `=`. Bash
treats this as an empty assignment followed by an attempt to execute the token
as a command. Literal `--token` values also enter shell history and process
arguments. Do not repair either problem by manually editing `accounts.json` or
re-entering a secret on the command line.

#### Stale `account.scopes` routing the wrong code path

`account.scopes` is captured from the provider credential boundary. If a token
was imported through the wrong workflow, the saved metadata can route usage to
the wrong endpoint:

| Saved `scopes` | New token shape | Dispatcher sends to | Result |
|---|---|---|---|
| includes `user:profile` | full-scope OAuth | `/api/oauth/usage` | 200 |
| includes `user:profile` | `setup-token` (no profile) | `/api/oauth/usage` | **403/401** |
| no `user:profile` | full-scope OAuth | `/v1/messages` | 200 (works either way) |
| no `user:profile` | `setup-token` | `/v1/messages` | 200 |

Reimporting the current OAuth login refreshes its scope metadata. The dedicated
setup-token command records an inference-only credential without relying on a
different active local login.

### Fix

Use the provider-owned login flow that matches the credential type. These paths
capture credentials without placing them in command arguments:

```bash
# OAuth login
claude auth login
sidekick-usages refresh "your-label"

# Or create and save a long-lived setup token
sidekick-usages claude setup-token --label "your-label"

sidekick-usages --only claude check
```

If `check` still returns 401 after a clean reauthentication:

1. Run the direct curl probe above. If that 401s too, the token is
   genuinely dead — mint a new one.
2. If the curl returns 200 but `sidekick-usages` 401s, use
   `sidekick-usages doctor --json` to confirm the selected store and credential
   classification, then repeat the supported import path. Do not print or edit
   the stored token. See the
   [persistence location guide](../persistence-and-recovery.md) before assuming
   the 0.6.0 compatibility path is active.

---

## Template: copy this when adding a new entry

```markdown
## <one-line symptom phrased as a heading>

<one-paragraph orientation: what triggers this, when you'd hit it,
and what this entry covers.>

### Symptom

<exact terminal output or behavior — copy-paste, don't paraphrase.>

### Don't be fooled

<false leads with links to owning source symbols.>

### Diagnostic

<the probe that isolates the symptom from sidekick-usages's code
path. Show the command and how to read the output.>

### <entry-specific> root causes

<the real explanations, ranked by frequency.>

### Fix

<specific commands, with redacted secrets.>
```

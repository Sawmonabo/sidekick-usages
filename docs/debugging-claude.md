# Debugging Claude accounts

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
   aren't. Cite source lines (`path/to/file.py:NNN`) so the reader
   can confirm.
3. **Diagnostic** — a probe that isolates the symptom from
   `sidekick-usages`'s code path (usually a direct curl). Includes
   how to read the output.
4. **Root causes** — the real explanations, ranked by frequency.
5. **Fix** — the specific commands to run, with redacted secrets.

Redact tokens (`sk-ant-oat01-<REDACTED>`) and anonymize identifiers
(`<ORG_UUID_A>`) when including worked examples. The technique is
the reusable content; the secret it was applied to is not.

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
Token refresh failed: Rate limited (HTTP 429) after 4 attempts.
Token refresh failed: HTTP 403 Forbidden (no body).
Token refresh failed: Claude CLI refresh failed: Login failed: Request failed with status code 400
```

### Don't be fooled

The `sk-ant-ort01-...` refresh token can be real and still fail if
the refresh request does not match Claude Code's own OAuth client
flow. Claude Code 2.1.174 uses:

- token endpoint: `https://platform.claude.com/v1/oauth/token`
- client id: `9d1c250a-e61b-44d9-88ed-5944d1962f5e`
- JSON body fields: `grant_type`, `refresh_token`, `client_id`,
  `scope`, and sometimes `expires_in`

Older direct-refresh code used `https://api.anthropic.com/v1/oauth/token`
with the metadata-document client id
`https://claude.ai/oauth/claude-code-client-metadata`. That request
shape is not equivalent to the installed CLI.

Even after matching the visible body fields, Python `urllib` requests
can still hit edge behavior that the Claude binary does not: dummy
token probes returned Cloudflare 1010 without the Claude Code user
agent and Anthropic `rate_limit_error` 429 with it. The installed
`claude auth login --claudeai` path succeeded with the same saved
refresh token in an isolated temporary `HOME`.

### Diagnostic

Use the installed Claude binary itself, but isolate it from your real
`~/.claude` login:

```bash
tmp="$(mktemp -d)"
CLAUDE_CODE_OAUTH_REFRESH_TOKEN='sk-ant-ort01-<REDACTED>' \
CLAUDE_CODE_OAUTH_SCOPES='user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload' \
HOME="$tmp" \
claude auth login --claudeai

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

### Root causes

1. Direct refresh request shape drifted from Claude Code's current
   OAuth client metadata.
2. The platform token endpoint behaves differently for the official
   Claude binary than for Python `urllib`, even with similar visible
   JSON fields.
3. Some saved refresh tokens are genuinely dead. In that case the
   installed Claude binary rejects them too, usually with status 400.

### Fix

Current `sidekick-usages` refreshes saved Claude OAuth accounts by
running:

```text
claude auth login --claudeai
```

inside a temporary `HOME` with
`CLAUDE_CODE_OAUTH_REFRESH_TOKEN` and `CLAUDE_CODE_OAUTH_SCOPES` set
from the saved account. It then parses the temporary
`.claude/.credentials.json`, imports the rotated access/refresh
tokens into `~/.config/sidekick-usages/accounts.json`, and removes
the temporary home. Your real `~/.claude` login is not overwritten.

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
`claude login` + `sidekick-usages refresh <label>`), and
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
`account.plan`, which is parsed from the local Claude CLI keychain
entry (`oauth.get("subscriptionType")` at
`src/sidekick_usages/providers/claude.py:215`) and used only for
color-coding in `src/sidekick_usages/render.py:33-37`
(`PLAN_COLORS = {"max": "magenta", "team": "cyan", "pro": "green"}`).

It is **not** consulted during auth. The dispatch at
`providers/claude.py:236-238` routes on `account.scopes`, not on plan:

```python
if account.scopes is not None and PROFILE_SCOPE not in account.scopes:
    return self._fetch_via_headers(account, http)   # /v1/messages probe
return self._fetch_via_oauth_endpoint(account, http) # /api/oauth/usage
```

Both code paths send the **same token bytes** as
`Authorization: Bearer …`. There is no `--plan max` flag that changes
the request. If the plan label is wrong on a saved account, that's a
display bug — it cannot cause a 401.

#### 2. "Token expired" isn't always token expiry

Anthropic's API returns 401 for any malformed `Authorization` header,
not just rotated/revoked tokens. The two most common non-expiry
causes (see [Root causes](#root-causes) below):

- whitespace in the stored token bytes (leading space, trailing `\n`,
  shell-quoting accidents);
- a stale `scopes` field on the saved account that routes the request
  down the wrong code path.

### Diagnostic: probe the token directly

The first thing to verify is whether the token itself works against
Anthropic's API, independent of anything `sidekick-usages` stored or
sent. `/v1/messages` is the same endpoint the `_fetch_via_headers`
path uses (`providers/claude.py:296-309`), so a direct curl bypasses
every layer of this tool:

```bash
curl -s -i -X POST https://api.anthropic.com/v1/messages \
  -H "Authorization: Bearer sk-ant-oat01-<REDACTED>" \
  -H "anthropic-version: 2023-06-01" \
  -H "anthropic-beta: oauth-2025-04-20" \
  -H "User-Agent: claude-code/2.0.32" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,
       "messages":[{"role":"user","content":"q"}]}' \
  | grep -iE "(^HTTP/|anthropic-ratelimit-|anthropic-organization-id|error)"
```

- **HTTP 200** → the token is valid; the 401 is something inside
  `sidekick-usages` (whitespace, stale scopes, wrong account
  selected). Jump to [Root causes](#root-causes).
- **HTTP 401** → the token is genuinely revoked/expired/wrong. Mint
  a new one (`claude setup-token` or `claude login`).
- **HTTP 403** → the token is valid but missing a scope the endpoint
  requires. Usually means a `setup-token` (inference-only) is being
  pointed at `/api/oauth/usage` instead of `/v1/messages`.

### What the response headers reveal

When the probe returns 200, the response headers are gold. The four
most useful:

| Header | What it tells you |
|---|---|
| `anthropic-organization-id` | UUID of the Anthropic org this token belongs to. Personal accounts are single-user orgs with their own UUID. **This is the cleanest "are these two tokens the same account?" signal Anthropic exposes** — compare UUIDs, not display names. |
| `anthropic-ratelimit-unified-overage-status` | `allowed` or `rejected`. `rejected` is a strong Team/Enterprise-plan fingerprint (see below). |
| `anthropic-ratelimit-unified-{5h,7d}-utilization` | Current usage in each window. Sanity-checks that the token is actively being used by the account you think it is. |
| `anthropic-ratelimit-unified-{5h,7d}-reset` | Unix timestamp of next reset. Different accounts drift to different reset times based on first-use-in-window, so two distinct accounts will have distinct reset minutes. |

#### Distinguishing Team plan from personal/Max

If `overage-status` is `rejected`, the response also carries an
`overage-disabled-reason`. Two values worth knowing:

- **`group_zero_credit_limit`** → a workspace admin on a Team /
  Enterprise plan has set the org's overage spend to $0. This is a
  config you **cannot** apply to a personal plan. Seeing this confirms
  the token belongs to an org-administered account.
- **`usage_limit_reached`** → personal account has hit its monthly
  cap.

The `group_*` prefix specifically means "an organization-level
setting blocked this," not a user-level setting.

#### Worked example

Two tokens probed at the same time, both returning 200:

| Label | `anthropic-organization-id` | `overage-status` | Reason | Conclusion |
|---|---|---|---|---|
| Work Org | `<ORG_UUID_A>` | `rejected` | `group_zero_credit_limit` | Team plan; admin disabled overage |
| Personal Max | `<ORG_UUID_B>` | `allowed` | — | Personal Max plan; pay-as-you-go overage permitted |

Three independent signals confirm these are distinct accounts and
that the labels are correct:

1. Different `anthropic-organization-id` UUIDs — if the labels were
   swapped or pointed at the same account, one or both would match.
2. `group_zero_credit_limit` on the Org token is a Team-plan-only
   marker — impossible to set on a personal plan.
3. Utilization values differ in a way consistent with the labels
   (work account showed recent activity; personal showed near-zero).

If the labels in your config disagree with the org IDs the API
returns, that's the bug — rename or replace the account.

### Root causes

When the direct curl returns 200 but `sidekick-usages` still 401s,
one of these is almost always the cause.

#### Whitespace in stored token bytes

The most common path to a "phantom" 401: the saved `access_token`
contains a leading space, trailing newline, or shell-quoting artifact.
Anthropic rejects `Authorization: Bearer  sk-ant-…` (double space)
with 401.

The classic source is `export X= sk-ant-…` — note the space after
`=`. Bash treats this as `export X=` (assigning empty) followed by an
attempt to execute `sk-ant-…` as a command. If you then copy that
same pasted string into `sidekick-usages add --token "..."`, the
literal leading space rides along into the saved bytes.

**Whitespace-safe save** (no shell-quoting hazards, no terminal echo
of the token):

```bash
printf '%s' 'sk-ant-oat01-<REDACTED>' \
  | sidekick-usages add claude --label "your-label" --force
```

`printf '%s'` emits the token with no trailing newline and no
interpretation of escape sequences — what goes in is exactly what
gets stored.

> Note on `add` precedence: when both stdin and a local Claude CLI
> login are present, `add` prefers the local-CLI auto-detect over the
> pipe. If your local `claude` CLI is logged into a *different*
> account than the token you piped, you'll silently save the wrong
> one. To force the piped token, use `--token` explicitly, or log out
> of `claude` first.

#### Stale `account.scopes` routing the wrong code path

`account.scopes` is captured at `add` time from the local CLI
keychain entry. If the saved value is stale — e.g., the keychain
entry was a full-scope OAuth login when you first ran `add`, but the
token you just refreshed in is a `setup-token` (inference-only) —
the dispatcher at `providers/claude.py:236-238` will route to the
wrong endpoint:

| Saved `scopes` | New token shape | Dispatcher sends to | Result |
|---|---|---|---|
| includes `user:profile` | full-scope OAuth | `/api/oauth/usage` | 200 |
| includes `user:profile` | `setup-token` (no profile) | `/api/oauth/usage` | **403/401** |
| no `user:profile` | full-scope OAuth | `/v1/messages` | 200 (works either way) |
| no `user:profile` | `setup-token` | `/v1/messages` | 200 |

Re-running `add … --force` rewrites `scopes` from the current
keychain entry, which is why it tends to fix mysterious 401s. If
you're piping a `setup-token` and don't have the matching CLI session
locally, pass `--token` explicitly so `add` doesn't read stale
keychain scopes.

### Fix

For most real-world 401s, this one-liner clears it:

```bash
# 1. Refresh from whichever source has the canonical token bytes
#    (local CLI keychain OR a piped setup-token)
printf '%s' 'sk-ant-oat01-<REDACTED>' \
  | sidekick-usages add claude --label "your-label" --force

# 2. Verify
sidekick-usages --only claude check
```

If `check` still 401s after a clean `--force`:

1. Run the direct curl probe above. If that 401s too, the token is
   genuinely dead — mint a new one.
2. If the curl returns 200 but `sidekick-usages` 401s, inspect
   `~/.config/sidekick-usages/accounts.json` for whitespace in
   `access_token`, then re-run step 1.

---

## Template: copy this when adding a new entry

```markdown
## <one-line symptom phrased as a heading>

<one-paragraph orientation: what triggers this, when you'd hit it,
and what this entry covers.>

### Symptom

<exact terminal output or behavior — copy-paste, don't paraphrase.>

### Don't be fooled

<false leads with source-line citations.>

### Diagnostic

<the probe that isolates the symptom from sidekick-usages's code
path. Show the command and how to read the output.>

### Root causes

<the real explanations, ranked by frequency.>

### Fix

<specific commands, with redacted secrets.>
```

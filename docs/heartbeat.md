# Usage-window heartbeat

Heartbeat is optional usage-window warming. It is separate from token
refresh.

Token refresh keeps saved access tokens valid. Heartbeat intentionally
sends a tiny provider request to open an inactive usage window so
`sidekick-usages` can show an active 5-hour reset time before a work
session starts.

Heartbeat does not create free quota. A successful warm is a real model request,
appears as provider usage, and consumes a small amount of your quota.

## Commands

```bash
sidekick-usages heartbeat <label>
sidekick-usages heartbeat <label> --target spark
sidekick-usages heartbeat enable <label>
sidekick-usages heartbeat enable <label> --target all
sidekick-usages heartbeat disable <label>
sidekick-usages heartbeat status
sidekick-usages heartbeat status --json
sidekick-usages heartbeat --all --quiet
sidekick-usages maintain --quiet
```

`sidekick-usages heartbeat <label>` runs one explicit warm attempt for a
saved account. It does not enable daemon heartbeat.

`sidekick-usages heartbeat enable <label>` opts one supported account
into daemon heartbeat. `sidekick-usages heartbeat disable <label>` turns
it back off.

`sidekick-usages heartbeat --all --quiet` is scheduler-safe. It only
checks accounts with heartbeat enabled and prints only accounts that
need manual action.

`sidekick-usages maintain --quiet` is what the daemon runs. It refreshes
saved tokens first, then runs heartbeat for enabled accounts.

## Supported providers

Claude OAuth/team accounts are supported when the saved token has both
`user:profile` and `user:inference`:

- `user:profile` lets sidekick read `/api/oauth/usage`.
- `user:inference` lets sidekick send the tiny `/v1/messages` warming
  request with `claude-haiku-4-5-20251001`.

Claude setup-token or inference-only accounts are also supported, but
they cannot read `/api/oauth/usage`. For those accounts, heartbeat uses
the same tiny `/v1/messages` header probe used by the usage fallback.

Codex ChatGPT-login accounts are supported when the saved account has a
usable access token and ChatGPT account id. Heartbeat reads
`https://chatgpt.com/backend-api/codex/usage` first. Codex has two
relevant window targets:

- `standard`: the primary Codex 5-hour window. This is the default and
  warms with `gpt-5.4-mini`, the cheaper eligible standard Codex model.
- `spark`: the separate GPT-5.3-Codex-Spark 5-hour window. This is not
  enabled by default and warms with `gpt-5.3-codex-spark`.

Use `--target spark` for a one-shot Spark warm, or
`heartbeat enable <label> --target all` if you intentionally want the
daemon to keep both the standard and Spark windows warm. Sidekick reads
usage again after each model request and only reports `warmed` when the
target window becomes active.

Codex API-key mode is not heartbeat supported. The heartbeat implementation
is for saved ChatGPT OAuth accounts whose usage is displayed by the Codex
usage endpoint.

## Daemon behavior

Heartbeat is default-off and per-account opt-in.

Every daemon tick runs:

```bash
sidekick-usages maintain --quiet
```

That command:

1. Refreshes due saved tokens using stored refresh tokens.
2. Checks heartbeat-enabled accounts.
3. Skips accounts with a cached future 5-hour reset.
4. Sends a tiny warming request only when the account is supported and
   the 5-hour window appears inactive.

The v1 policy is any time. If heartbeat is enabled and the scheduler
finds an inactive supported 5-hour window, it may warm it regardless of
time of day. Disable heartbeat for an account if that is not what you want:

```bash
sidekick-usages heartbeat disable <label>
```

## Guardrails

- Default off.
- Per-account opt-in.
- Uses saved sidekick account credentials only.
- Never imports or overwrites from the current global Claude or Codex
  login.
- Does not run for accounts whose last token refresh failed.
- Does not probe again while the target-specific cached reset is still
  in the future. The legacy `heartbeat_5h_reset_at` field mirrors the
  default `standard` target for backward compatibility.
- Does not retry aggressively after provider errors.
- Records last heartbeat status and error on the account.

## Troubleshooting

Run:

```bash
sidekick-usages doctor
sidekick-usages heartbeat status
```

Common states:

- `heartbeat: off`: the account supports heartbeat, but daemon warming
  is disabled.
- `heartbeat: on`: daemon warming is enabled for the account.
- `heartbeat: unsupported`: the provider or saved account scopes cannot
  safely warm a window.
- `heartbeat: needs-login`: the last heartbeat failed because auth or
  refresh is broken.

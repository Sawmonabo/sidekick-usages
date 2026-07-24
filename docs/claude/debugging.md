# Claude account debugging

This guide diagnoses saved Claude accounts without printing credentials,
probing private provider endpoints by hand, or changing the active Claude
login implicitly.

## Start with read-only state

Run:

```bash
sidekick-usages doctor --provider claude
sidekick-usages doctor --provider claude --json
sidekick-usages list
```

`doctor` distinguishes the saved credential kind, usage route,
access-token expiry, login expiry, identity availability, auto-refresh
capability, refresh result, heartbeat state, and whether manual action is
required. It does not emit credential values, provider identities, emails, or
scope lists.

Do not manually inspect or edit `accounts.json`, a Claude credential file, or
refresh staging. Manual copies bypass variant, identity, permission,
serialization, and recovery checks.

## Read the two lifetimes correctly

A subscription-login credential has two independent clocks:

- **access-token expiry** is the short-lived credential deadline. A usable
  saved refresh credential can rotate it without replacing the login.
- **login expiry** is the refresh credential deadline. Access refresh does not
  prove that the underlying login remains renewable forever.

Sidekick emits a five-day login-renewal warning when a known, still-valid
login expiry is at or inside five days. Exact five days is included. The
warning is a derived manual-action state, not a failed refresh, and does not
overwrite `last_refresh_status` or `last_refresh_error`.

An expired or invalid login lifetime fails closed before a provider exchange.
An unknown login expiry stays unknown and produces no proximity warning. A
setup token has no login expiry at all; its provider-issued creation time is
not encoded in the token, so Sidekick cannot recover or infer it.

## Match the recovery action to the credential mode

Every rendered authentication failure must contain one cause.
It must also contain one recovery action.

### Setup token rejected

Cause:

```text
Claude rejected the saved setup token.
```

Recovery action:

```bash
sidekick-usages claude setup-token --label <label> --force
```

This captures a new setup token. It does not import the active subscription
login.

### Subscription login rejected or expiring

First sign in to the same Claude account intentionally:

```bash
claude auth login
```

Then import that matching login into the subscription-login label:

```bash
sidekick-usages refresh <label>
```

Do not use `--replace-identity` to silence a mismatch. That flag authorizes a
real identity change. Do not use `--replace-auth-method` unless intentionally
converting a setup-token label to a subscription login.

## Refresh failure causes

Claude refresh returns bounded, cause-only states. The provider adapter does
not append recovery prose. Current causes distinguish:

- missing refresh credential;
- access credential expired;
- login credential expired;
- provider rejected refresh;
- refresh timed out;
- refresh process unavailable;
- refresh output incomplete;
- refresh output malformed;
- refreshed identity mismatch; and
- refresh temporarily unavailable.

CLI and usage renderers select the single recovery action from the saved
credential mode. Raw process output, provider payloads, environment values,
credential bytes, and stable identity values are excluded from diagnostics.

## Command transition failures

`sidekick-usages refresh <label>` imports the current local login. The command
fails before persistence when any required authority is absent:

| Intended change | Required authorization |
| --- | --- |
| Refresh the same proven login | None |
| Replace the known or unprovable login identity | `--replace-identity` |
| Convert setup token to subscription login | `--replace-auth-method` |
| Convert setup token and introduce a different identity | Both replacement flags |

Converting a subscription login to a setup token uses the setup-token command.
It requires the explicit overwrite flow and independent identity authority.
The saved plan and heartbeat history remain account state; login-only refresh
metadata is cleared because it cannot exist on the setup-token variant.

## Maintenance and recovery states

For scheduler behavior, run the same path as the daemon:

```bash
sidekick-usages maintain --quiet
sidekick-usages doctor
sidekick-usages daemon status
```

Maintenance uses saved credentials only. It never adopts the active Claude
login. Refreshes sharing one credential are serialized before provider
traffic, and a complete private stage can be recovered locally without a
second provider request. If doctor reports blocked refresh recovery, preserve
the evidence and follow its emitted action; do not delete journals or staging
directories by hand.

## Safe support report

When asking for help, include only:

- the `sidekick-usages` and Claude Code versions;
- the command name and exit code;
- credential kind and expiry states from redacted doctor output;
- the bounded cause text; and
- the operating-system family.

Remove labels and local paths. Never include account files, credential files,
provider responses, terminal capture from authentication, or full environment
output.

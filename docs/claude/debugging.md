# Claude account debugging

This guide diagnoses saved Claude accounts without printing credentials,
probing private provider endpoints by hand, or changing the active Claude
login implicitly.

## Start with read-only state

Run:

```bash
sidekick-usages
sidekick-usages doctor --provider claude
sidekick-usages doctor --provider claude --json
```

`doctor` distinguishes the saved credential kind, usage route,
access-token expiry, login expiry, identity availability, auto-refresh
capability, refresh result, heartbeat state, and whether manual action is
required. It does not emit credential values, provider identities, emails, or
scope lists.

The session diagnostic separates saved accounts from runtime participants. A
saved account affects the Claude panel count; integrated, unreachable, lost,
and unmanaged sessions are status only. An unmanaged session remains alive
and may continue using the authority it already resolved. It is never shown as
an external account or counted as globally converged.

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
setup token has no refresh authority. Sidekick records its one-year access
lifetime only when it owns the capture or renewal event; older tokens without
trusted capture evidence remain unknown.

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

Repair the matching subscription-login label:

```bash
sidekick-usages refresh <label>
```

The provider may open browser, MFA, password, or consent UI for that private
profile. Do not use `--replace-identity` to silence an established mismatch.
For a setup-token-only account, the flag explicitly approves the first
subscription identity association while retaining the setup token.

## Refresh failure causes

Claude maintenance returns bounded, cause-only states from its official
private-profile operation. Current states distinguish:

- official Claude missing, unsupported, timed out, or unavailable;
- protected credentials missing, unreadable, incomplete, or malformed;
- login lifetime expired;
- identity or generation mismatch requiring reconciliation; and
- provider login failure with the existing authority left unchanged.

CLI and usage renderers select the single recovery action from the saved
credential mode. Raw process output, provider payloads, environment values,
credential bytes, and stable identity values are excluded from diagnostics.

## Command transition failures

`sidekick-usages refresh <label>` uses only the label's private managed
profile. The command fails before persistence when required proof is absent:

| Intended change | Required authorization |
| --- | --- |
| Refresh the same proven login | None |
| Establish the first subscription identity on a setup-token-only account | `--replace-identity` |
| Replace an established subscription identity | Refused; reconcile the saved account |

Capturing a setup token uses the setup-token command and preserves any
subscription authority. The saved plan, metrics, and heartbeat history remain
attached to the same stable account ID.

## Maintenance and recovery states

For scheduler behavior, run the same path as the daemon:

```bash
sidekick-usages maintain --quiet
sidekick-usages doctor
sidekick-usages daemon status
```

Maintenance enrolls every saved account independently of selection. It uses
the exact account's private authority and, for the selected account, separately
maintains the verified native authority for the same identity. Both operations
are serialized under that account's lock. Maintenance never adopts another
native identity or changes account selection. If doctor reports blocked
recovery, preserve the evidence and follow its emitted action; do not delete
journals or private profiles by hand.

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

For account-change failures, include the redacted provider phase, target label,
epoch, exact required, ready, adopted, lost, and unreachable counts, plus the
unmanaged-status availability. An unmanaged count is unavailable until a
provider-neutral owner exists. Do not include participant identifiers. For
setup-token selection, also report whether at least one Sidekick-owned
structured participant is registered and exact-build qualified. No participant
or an unqualified build produces a visible refusal before mutation. Mixed
refreshable selection must report native proof before protected installation.
A wait during active or background work, tools, hooks, permissions, dialogs,
MCP operations, or terminal children is expected; Sidekick must not interrupt
that work. The same provider PID and conversation must remain after a
successful switch. The feature-branch contract does not imply that the
currently installed release has completed its qualified cutover.

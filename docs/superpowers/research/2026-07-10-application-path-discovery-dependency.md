# Application Path Discovery Dependency Decision

- *Change set:* CS-09
- *Research date:* 2026-07-10
- *Repository:* `sidekick-usages`
- *Python target:* 3.14
- *Status:* **WAITING FOR OPERATOR DECISION**
- *Recommendation:* Conditional GO for `platformdirs` 4.10.0 as the native
  path-discovery dependency; preserve compatibility locations until the later
  persistence and native-migration gates pass

## Decision Question

Should Sidekick Usages adopt `platformdirs` 4.10.0 behind `paths.py` to
discover native per-user data and cache locations on Linux, macOS, Windows,
and WSL?

This decision selects a path-discovery dependency and freezes its intended
outputs. It does not authorize moving an existing account store, rewriting a
stored schema, copying private credentials, or deleting any current file.

## Executive Recommendation

Record a conditional GO for `platformdirs` 4.10.0 with the exact constructor,
semantic mapping, output matrix, and override policy in this document.

The library is a focused, dependency-free implementation of the operating
system conventions the project would otherwise need to maintain itself. The
4.10.0 release is MIT licensed, requires Python 3.10 or newer, declares Python
3.14 support, publishes a universal wheel, and has verifiable PyPI publishing
provenance. Its scope matches the problem without introducing a settings
framework.

The GO must remain conditional because native paths differ from the current
public file locations. Initial `ApplicationPaths` centralization must preserve
the existing locations. A later migration may activate the frozen native paths
only after the persistence, credential, platform, recovery, and operator gates
listed below pass.

## Repository Ground Truth

The current repository constructs Sidekick-owned paths in multiple modules:

| Semantic role | Current owner | Current location |
|---|---|---|
| Account store | `src/sidekick_usages/store.py` | `~/.config/sidekick-usages/accounts.json` |
| Prototype import source | `src/sidekick_usages/store.py` | `~/.config/cc-usage/accounts.json` |
| Private Codex root | `src/sidekick_usages/cli.py` | `~/.config/sidekick-usages/codex/` |
| Private Codex auth bundle | Codex credential workflow | `~/.config/sidekick-usages/codex/<filesystem-safe-label>/auth.json` |
| Codex lifetime index | `src/sidekick_usages/lifetime.py` | `~/.config/sidekick-usages/codex-lifetime-cache.json` |

The current account store and private Codex bundles contain durable account or
credential state. The lifetime index is derived from provider-native session
data and can be regenerated.

Provider-native locations are not Sidekick-owned application paths:

- Claude statistics remain provider-owned at
  `~/.claude/stats-cache.json`.
- Codex session logs remain provider-owned at
  `~/.codex/sessions/**/rollout-*.jsonl`.
- A user-selected or provider-native `CODEX_HOME` remains provider-owned.
- Scheduler installation locations remain owned by the daemon adapter.

`ApplicationPaths` must not absorb those provider or scheduler locations.

At the research commit, the project declares Click, Typer, and Rich as direct
runtime dependencies. `platformdirs` 4.9.6 appears only transitively in the
development lock graph. Transitive development availability is not a runtime
dependency contract.

## Reuse Before Building

### Options Considered

| Option | Benefit | Cost or risk | Disposition |
|---|---|---|---|
| Keep `Path.home() / ".config"` everywhere | No new dependency; no immediate relocation | Ignores native macOS and Windows conventions, conflates durable data and cache, and preserves duplicate path construction | Compatibility baseline only |
| Implement local per-platform discovery | No runtime dependency | Sidekick owns XDG validation, macOS conventions, Windows known-folder APIs, roaming behavior, WSL behavior, overrides, and future platform changes | Rejected |
| Adopt `platformdirs` 4.10.0 behind `paths.py` | Focused data/config/cache/state discovery, small pure-Python package, no dependencies, current Python support | Native paths differ from the existing public contract and require a separate migration | Recommended conditionally |
| Adopt a settings framework for path discovery | Could later combine settings sources and validation | Solves a broader problem than path discovery and introduces an unjustified settings model | Rejected for CS-09 |

### Package Evidence

The following facts were refreshed on 2026-07-10:

- PyPI identifies 4.10.0 as the current release and records its release date as
  2026-05-28.
- Package metadata requires Python 3.10 or newer and includes a Python 3.14
  classifier.
- The distribution uses the MIT license, publishes a universal
  `py3-none-any` wheel, and declares no runtime dependencies.
- PyPI displays Trusted Publishing provenance connecting the release artifact
  to the canonical `tox-dev/platformdirs` project.
- The canonical project was active at the research date.
- The project security page documents its supported-version policy and showed
  no published advisories at the research date. This is not a claim that no
  undisclosed vulnerability exists.

Primary evidence:

- [platformdirs on PyPI][platformdirs-pypi]
- [platformdirs canonical repository][platformdirs-repository]
- [platformdirs 4.10.0 release][platformdirs-release]
- [platformdirs security policy and advisories][platformdirs-security]
- [platformdirs API][platformdirs-api]
- [platformdirs parameters][platformdirs-parameters]

### Buy-versus-Build Conclusion

Adopt the maintained library rather than recreate its platform matrix.
Sidekick should own only its application-specific semantic mapping, strict
override validation, compatibility transition, and typed path value.

`platformdirs` remains a private implementation detail of `paths.py`.
Providers, persistence, services, CLI commands, core policy, and presentation
consume concrete `pathlib.Path` values and never import the dependency.

## Frozen Discovery Contract

Only `paths.py` may construct the production directory provider:

```python
PlatformDirs(
    appname="sidekick-usages",
    appauthor=False,
    version=None,
    roaming=False,
    multipath=False,
    opinion=True,
    ensure_exists=False,
    use_site_for_root=False,
)
```

The parameters are intentional:

- `appname="sidekick-usages"` preserves the product's established directory
  name.
- `appauthor=False` prevents the default Windows
  `sidekick-usages/sidekick-usages` duplication.
- `version=None` prevents application releases from moving persistent files to
  a new versioned directory.
- `roaming=False` keeps credential-bearing data in Windows Local AppData
  rather than roaming profiles.
- `multipath=False` selects one per-user location rather than a search path.
- `opinion=True` retains the library's Windows `Cache` child for cache data.
- `ensure_exists=False` makes discovery read-only.
- `use_site_for_root=False` keeps discovery in the per-user contract when a
  Unix process runs as root.

The official API documents `appauthor=False`, `roaming`, `opinion`, and
`ensure_exists` behavior. The 4.10.0 tagged source is the implementation
reference for the frozen release:

- [platformdirs 4.10.0 API source][platformdirs-api-source]
- [platformdirs 4.10.0 Unix source][platformdirs-unix-source]
- [platformdirs 4.10.0 macOS source][platformdirs-macos-source]
- [platformdirs 4.10.0 Windows source][platformdirs-windows-source]

## Exact Directory Outputs

An isolated 4.10.0 probe instantiated the Unix, macOS, and Windows platform
classes with controlled home or known-folder inputs. The Windows probe used
Windows path joining and separators. WSL intentionally used the Unix class,
matching its Linux Python runtime.

The following values are frozen as the expected output matrix:

| Environment | User data | User cache | User config | User state |
|---|---|---|---|---|
| Linux default | `/home/alice/.local/share/sidekick-usages` | `/home/alice/.cache/sidekick-usages` | `/home/alice/.config/sidekick-usages` | `/home/alice/.local/state/sidekick-usages` |
| Linux with absolute XDG roots | `/srv/alice/data/sidekick-usages` | `/srv/alice/cache/sidekick-usages` | `/srv/alice/config/sidekick-usages` | `/srv/alice/state/sidekick-usages` |
| macOS default | `/Users/alice/Library/Application Support/sidekick-usages` | `/Users/alice/Library/Caches/sidekick-usages` | `/Users/alice/Library/Application Support/sidekick-usages` | `/Users/alice/Library/Application Support/sidekick-usages` |
| Windows default | `C:\Users\Alice\AppData\Local\sidekick-usages` | `C:\Users\Alice\AppData\Local\sidekick-usages\Cache` | `C:\Users\Alice\AppData\Local\sidekick-usages` | `C:\Users\Alice\AppData\Local\sidekick-usages` |
| WSL default | `/home/alice/.local/share/sidekick-usages` | `/home/alice/.cache/sidekick-usages` | `/home/alice/.config/sidekick-usages` | `/home/alice/.local/state/sidekick-usages` |

These are deterministic class-level measurements. They are not a substitute
for the native operating-system acceptance runs required before activation.

Microsoft recommends keeping Linux command-line work in the WSL Linux
filesystem rather than a mounted Windows filesystem. The WSL contract
therefore follows Linux/XDG locations and never redirects Sidekick's Linux
process to `%LOCALAPPDATA%` or `/mnt/c` by default.

See [Microsoft's WSL filesystem guidance][wsl-filesystems].

## Semantic File Mapping

Native discovery maps files by lifecycle rather than by their current
directory name.

| Artifact | Semantic class | Native base |
|---|---|---|
| `accounts.json` | Durable, credential-bearing application data | `user_data_path` |
| Sidekick private Codex root and `auth.json` bundles | Durable, credential-bearing application data | `user_data_path` |
| `codex-lifetime-cache.json` | Regenerable derived cache | `user_cache_path` |

No current Sidekick artifact uses `user_config_path`, because the application
does not yet have a cohesive user-settings contract. No current artifact uses
`user_state_path`. A directory called `.config` in the old implementation does
not make account credentials ordinary settings.

Apple distinguishes application support data from recreatable cache data. The
mapping above follows that lifecycle distinction rather than treating every
file as configuration. See [Apple's filesystem directory guidance][apple-dirs].

### Exact Native Files

| Environment | Account file | Private Codex root | Lifetime cache file |
|---|---|---|---|
| Linux default | `~/.local/share/sidekick-usages/accounts.json` | `~/.local/share/sidekick-usages/codex/` | `~/.cache/sidekick-usages/codex-lifetime-cache.json` |
| Linux with absolute XDG roots | `$XDG_DATA_HOME/sidekick-usages/accounts.json` | `$XDG_DATA_HOME/sidekick-usages/codex/` | `$XDG_CACHE_HOME/sidekick-usages/codex-lifetime-cache.json` |
| WSL | Same Linux/XDG contract below the WSL Linux home | Same Linux/XDG data contract | Same Linux/XDG cache contract |
| macOS default | `~/Library/Application Support/sidekick-usages/accounts.json` | `~/Library/Application Support/sidekick-usages/codex/` | `~/Library/Caches/sidekick-usages/codex-lifetime-cache.json` |
| Windows default | `%LOCALAPPDATA%\sidekick-usages\accounts.json` | `%LOCALAPPDATA%\sidekick-usages\codex\` | `%LOCALAPPDATA%\sidekick-usages\Cache\codex-lifetime-cache.json` |

An account's private auth file remains
`<private-codex-root>/<validated-relative-account-directory>/auth.json`.
Later migration must derive that relative directory from a validated existing
Sidekick-owned path. It must not recreate the destination from a sanitized
label and risk merging two accounts whose labels sanitize to the same name.

## Override Policy

### XDG Variables

Honor non-empty, absolute values for the relevant XDG variables on Linux, WSL,
and macOS, matching the measured platformdirs 4.10.0 behavior:

- `XDG_DATA_HOME`;
- `XDG_CACHE_HOME`;
- `XDG_CONFIG_HOME`; and
- `XDG_STATE_HOME`.

The [XDG Base Directory Specification][xdg-spec] requires these home variables
to be absolute. The isolated probe confirmed that platformdirs 4.10.0 returns a
relative result if given a relative XDG value. Sidekick must add its own
fail-closed check: if a relevant non-empty XDG value is relative, raise a typed
path-discovery error before reading or writing. Credentials must never resolve
relative to the process working directory.

### Windows Overrides

Normal Windows folder redirection comes from the operating system's known
folder result. `platformdirs` 4.10.0 also recognizes library-specific
`WIN_PD_OVERRIDE_*` environment variables before resolving a known folder.
Those variables are useful for library tests but are not a Sidekick product
configuration surface.

Production composition must reject non-empty `WIN_PD_OVERRIDE_*` variables.
Sidekick tests construct `ApplicationPaths` directly and do not need a hidden
production override. Operating-system folder redirection remains supported
through `SHGetKnownFolderPath`.

See [Microsoft's `SHGetKnownFolderPath` reference][known-folder].

### No Sidekick-Specific Override

CS-09 does not introduce a second Sidekick path environment variable, settings
file, global settings object, or command option. A new override would require
its own concrete precedence, security, migration, and support contract.

## Discovery Has No Side Effects

An isolated probe accessed user data, cache, config, and state properties for
Unix, macOS, and Windows classes beneath controlled absent roots with
`ensure_exists=False`.

The measured result was:

```json
{
  "ensure_exists_false_before": [],
  "ensure_exists_false_after": [],
  "side_effect_free": true
}
```

Path discovery creates no directory or file. Persistence, private-credential,
and cache writers create only their own approved destinations when performing
an authorized operation. Help and version do not require path discovery.

## Compatibility and Native Locations Are Separate Decisions

Initial path centralization preserves every current physical location:

- the account store remains
  `~/.config/sidekick-usages/accounts.json`;
- the prototype remains
  `~/.config/cc-usage/accounts.json`;
- private Codex bundles remain below
  `~/.config/sidekick-usages/codex/`; and
- the lifetime cache remains
  `~/.config/sidekick-usages/codex-lifetime-cache.json`.

During this compatibility phase, `ApplicationPaths` may expose the current
Sidekick location as both its selected and compatibility location. Consumers
stop reconstructing paths, but no user data moves.

The native path matrix in this document becomes a migration destination only
after a later, separately approved migration change. That later work must:

- treat the native and current Sidekick stores as distinct authoritative
  candidates;
- use the prototype only as an import fallback when neither authoritative
  candidate exists;
- fail closed when two authoritative candidates conflict;
- leave a stale prototype untouched after a valid authoritative store exists;
- validate and copy every Sidekick-owned private Codex bundle before rewriting
  its persisted location;
- leave external and provider-native Codex homes untouched;
- preserve credential permissions and Windows ACL protections;
- commit account state only after every required credential copy validates;
- retain old durable files and backups; and
- never silently merge, overwrite, or delete a durable source.

Native migration is not part of CS-09. This section records the minimum
compatibility boundary needed to judge whether the dependency can be adopted.

## Packaging and Platform Limits

### Packaging

A recorded GO authorizes a later change to declare `platformdirs` directly in
`pyproject.toml` and update `uv.lock`. It does not authorize relying on the
older transitive development copy.

The later packaging check must verify:

- the locked version satisfies the approved constraint;
- source distribution, wheel, editable install, and isolated wheel install;
- Homebrew packaging and installation;
- CLI startup and help latency;
- the built artifact imports `platformdirs` only through `paths.py`; and
- supported Linux, macOS, and Windows Python 3.14 environments.

No production dependency or dormant native-migration branch is added by this
research decision alone.

### Supported Boundary

The frozen outputs cover normal per-user local execution. They do not approve:

- site-wide or system paths;
- shared or network filesystems;
- containers that intentionally remap home directories without a documented
  per-user contract;
- relative XDG roots;
- Windows library test overrides;
- running the WSL CLI against a Windows-mounted data root by default; or
- migration of provider-native or scheduler-owned files.

Valid operating-system folder redirection and valid absolute XDG roots are
part of the supported contract.

## Later Blockers Outside CS-09

Native relocation remains blocked on the later stored-persistence and recovery
decision. That later decision must independently settle:

- the versioned account schema and every supported historical input;
- backup, collision, interruption, and restart behavior;
- cross-process coordination and old-process quiescence;
- platform-specific file and directory durability;
- existing Windows compatibility-directory ACL assessment;
- private Codex multi-file migration and restart behavior;
- read-only `doctor` recovery output; and
- lossless recovery for the previous released binary after newer writes.

This document does not select or freeze any schema envelope, lock dependency,
backup filename, write protocol, migration command, or rollback command. Those
are later decisions and remain blockers even if the operator approves CS-09.

## Activation Gates

`platformdirs`-backed native locations may become writable only when all of the
following are true:

1. The operator records CS-09 GO with this exact constructor, path matrix,
   semantic mapping, and override policy.
2. The dependency is declared directly and passes the packaging checks above.
3. Native Linux, macOS, Windows, and WSL runners reproduce the approved
   discovery behavior.
4. Side-effect-free discovery and relative-XDG rejection pass on every relevant
   platform.
5. The later persistence and recovery decision is approved and implemented.
6. Read-only assessment distinguishes absent, current, prototype, native,
   equivalent, conflicting, malformed, unreadable, and unsafe states before
   loading an account store.
7. Private Codex containment, collision, permission, ACL, partial-copy, and
   external-home behavior passes on native platforms.
8. A native migration restart is idempotent at every approved interruption
   point.
9. The latest representable state can be restored to the compatibility
   location and read by the previous released binary.
10. `doctor` reports blocked and recoverable states without exposing secrets.
11. A final operator activation decision records the platform and rollback
    evidence.

Before those gates pass, compatibility `ApplicationPaths` remain authoritative
and the native outputs are evidence, not active storage locations.

## Reversal Conditions

Reverse the preliminary adoption and retain compatibility paths if any of the
following occurs before activation:

- native operating-system output differs from the frozen matrix without an
  intentionally approved amendment;
- relative or otherwise unsafe override behavior cannot be rejected reliably;
- the package drops a supported Python or platform requirement;
- a relevant security advisory cannot be mitigated acceptably;
- maintenance, provenance, license, or release posture becomes unacceptable;
- wheel, source, Homebrew, startup, or supported-platform packaging fails;
- Windows known-folder, ACL, or path behavior cannot meet the credential
  contract;
- macOS, Linux, Windows, or WSL migration and recovery cannot pass their native
  gates; or
- the later persistence decision cannot prove compatibility and previous-binary
  recovery.

On reversal:

- keep the compatibility `ApplicationPaths` abstraction;
- keep every current physical location;
- remove any unneeded direct `platformdirs` dependency;
- omit native-only migration code and provider migration ports;
- retain no dormant branch for hypothetical later use; and
- continue unrelated architecture work against injected compatibility paths.

## Operator Decision Required

### Recommended GO

Approve `platformdirs` 4.10.0 as the later native path-discovery dependency with
the exact contract in this document. This GO authorizes direct dependency and
native-migration implementation only in their later gated change sets. It does
not move or rewrite user data.

### NO-GO Consequence

Retain the current physical locations through compatibility
`ApplicationPaths`, add no direct `platformdirs` dependency, and omit native
migration-only code. Other architectural refactors continue.

### Required Recorded Disposition

The operator record must include:

- GO or NO-GO;
- the selected compatibility disposition;
- approval date;
- the design commit that incorporates the disposition; and
- the approved design-content digest required by the implementation plan.

Until that disposition is recorded, CS-09 remains
**WAITING FOR OPERATOR DECISION**.

## Sources

All web sources were accessed on 2026-07-10.

[apple-dirs]: https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/FileSystemProgrammingGuide/MacOSXDirectories/MacOSXDirectories.html
[known-folder]: https://learn.microsoft.com/en-us/windows/win32/api/shlobj_core/nf-shlobj_core-shgetknownfolderpath
[platformdirs-api-source]: https://raw.githubusercontent.com/tox-dev/platformdirs/4.10.0/src/platformdirs/api.py
[platformdirs-api]: https://platformdirs.readthedocs.io/en/stable/api.html
[platformdirs-macos-source]: https://raw.githubusercontent.com/tox-dev/platformdirs/4.10.0/src/platformdirs/macos.py
[platformdirs-parameters]: https://platformdirs.readthedocs.io/en/latest/parameters.html
[platformdirs-pypi]: https://pypi.org/project/platformdirs/
[platformdirs-release]: https://github.com/tox-dev/platformdirs/releases/tag/4.10.0
[platformdirs-repository]: https://github.com/tox-dev/platformdirs
[platformdirs-security]: https://github.com/tox-dev/platformdirs/security
[platformdirs-unix-source]: https://raw.githubusercontent.com/tox-dev/platformdirs/4.10.0/src/platformdirs/unix.py
[platformdirs-windows-source]: https://raw.githubusercontent.com/tox-dev/platformdirs/4.10.0/src/platformdirs/windows.py
[wsl-filesystems]: https://learn.microsoft.com/en-us/windows/wsl/filesystems
[xdg-spec]: https://specifications.freedesktop.org/basedir-spec/latest/

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0](https://github.com/Sawmonabo/sidekick-usages/compare/v0.3.0...v0.4.0) (2026-06-12)


### Added

* **auth:** add token maintenance daemon ([f966394](https://github.com/Sawmonabo/sidekick-usages/commit/f966394e317e011f1983facacbb664040c43abdc))


### Fixed

* **hooks:** restore command-guard PreToolUse enforcement ([1915b5a](https://github.com/Sawmonabo/sidekick-usages/commit/1915b5a6572d9db3dda2ce4753f1a0635d3b704c))


### Docs

* add Claude provider debugging log and link from README ([0398c50](https://github.com/Sawmonabo/sidekick-usages/commit/0398c50c62089827e14a5a5fb26f29eca0711ff9))

## [0.3.0](https://github.com/Sawmonabo/sidekick-usages/compare/v0.2.0...v0.3.0) (2026-05-16)


### Added

* add check-update and update commands (and switch release-please to a PAT) ([#10](https://github.com/Sawmonabo/sidekick-usages/issues/10)) ([d9f3420](https://github.com/Sawmonabo/sidekick-usages/commit/d9f3420123909d4bf2dffc63a87750d767cc1b89))

## [0.2.0](https://github.com/Sawmonabo/sidekick-usages/compare/v0.1.0...v0.2.0) (2026-05-16)


### Added

* **homebrew:** automate formula regeneration on tag push ([#4](https://github.com/Sawmonabo/sidekick-usages/issues/4)) ([847a2e3](https://github.com/Sawmonabo/sidekick-usages/commit/847a2e363cce96962fa152001c12b9e222329769))


### Fixed

* fetch usage via response headers when scopes lack user:profile ([#6](https://github.com/Sawmonabo/sidekick-usages/issues/6)) ([fadb145](https://github.com/Sawmonabo/sidekick-usages/commit/fadb1452bdbfb37cf7cd889e991466a45c0d0fa0))
* **homebrew:** overlay generate.py from main before running ([#5](https://github.com/Sawmonabo/sidekick-usages/issues/5)) ([923c655](https://github.com/Sawmonabo/sidekick-usages/commit/923c655d94a2970cc9e86f418f14f5e03d393df6))

## [0.1.0] — 2026-05-13

Initial release.

### Added

- **Multi-account, multi-provider usage reporting.** Saves accounts
  for Claude Code and Codex CLI in a single config file and prints
  per-account utilization in one command.
- **Claude Code provider.** Auto-detects credentials from macOS
  Keychain (`Claude Code-credentials`), Linux/WSL files
  (`~/.claude/.credentials.json`), and Windows storage. Parses the
  4 OAuth usage buckets (`five_hour`, `seven_day`, `seven_day_opus`,
  `seven_day_oauth_apps`).
- **Codex CLI provider.** Reads `~/.codex/auth.json`, parses the
  `primary_window`, `secondary_window`, and per-model
  `additional_rate_limits` buckets, and **automatically refreshes
  expired access tokens** via the OpenAI OAuth token endpoint when
  a request returns 401.
- **Idempotent `add`.** Saving an already-saved token reuses the
  existing entry instead of duplicating it.
- **`setup-token`** subcommand wrapping `claude setup-token` for
  generating long-lived (one-year) OAuth tokens.
- **`refresh`** subcommand to pull the current local CLI login into
  a saved account.
- **`reset`** subcommand with an optional `--provider` filter for
  scoped wipes.
- **`--only`** global option to filter `check` output by provider.
- **Style C aligned renderer.** Braille-dot progress bars with
  column-aligned `[provider · plan]` tag aligned over the `↻` reset
  symbol. Built on Rich for proper ANSI-width handling.
- **Bootstrap installer** (`install.sh`) that ensures `uv` is
  available and installs the package as a global tool.
- **Homebrew tap recipe** at
  `packaging/homebrew/sidekick-usages.rb` for `brew tap`-based
  installs.
- **Auto-migration** from legacy `~/.config/cc-usage/accounts.json`
  on first run.

### Security

- Config file written with `chmod 600` on Unix.
- Token prompts use Rich's password input so tokens never appear
  on screen or in shell history.
- Token text filtered out of `setup-token` subprocess output before
  it's echoed to the terminal.

[0.1.0]: https://github.com/Sawmonabo/sidekick-usages/releases/tag/v0.1.0

# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/Sawmonabo/sidekick-usages/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Sawmonabo/sidekick-usages/releases/tag/v0.1.0

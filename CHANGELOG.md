# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.0](https://github.com/Sawmonabo/sidekick-usages/compare/v0.5.0...v0.6.0) (2026-06-20)


### Added

* **cli:** add set-plan command for manual plan overrides ([9c8ea80](https://github.com/Sawmonabo/sidekick-usages/commit/9c8ea8010fb6847fcf01f5a4a82e3dab148b57e6))
* **cli:** render check as grouped provider overview ([45096b3](https://github.com/Sawmonabo/sidekick-usages/commit/45096b3cde1a160a5fbcd657f4822cebddea827f))
* **cli:** reroute fetch errors from printed blocks to in-panel FetchFailure records ([3698770](https://github.com/Sawmonabo/sidekick-usages/commit/3698770f4846149b6b9be013aaff37ba16742e6d))
* **cli:** shell-quote the refresh recovery command label ([0f6beee](https://github.com/Sawmonabo/sidekick-usages/commit/0f6beee171d7e8b50ce2ade0be4d8497e5fdf773))
* **lifetime:** add cached Codex output summing ([2c56cfa](https://github.com/Sawmonabo/sidekick-usages/commit/2c56cfa040623c0cb808ec0a3703912ebe3ca5b8))
* **lifetime:** add token formatting + Claude output sum ([adfff0a](https://github.com/Sawmonabo/sidekick-usages/commit/adfff0a5bf4125f36be69b4cdb72e5f5eae92fcc))
* redesign usage check as framed heat panels ([680deb0](https://github.com/Sawmonabo/sidekick-usages/commit/680deb0349b7bad87c90f3d2398a4f2a1c9ea404))
* **render:** add a leading blank line above the overview ([73493bf](https://github.com/Sawmonabo/sidekick-usages/commit/73493bf760fae95344a70a203c1254e0c10c417e))
* **render:** add compact reset-countdown cell ([270f355](https://github.com/Sawmonabo/sidekick-usages/commit/270f3552726ea739a16aa15f02611946dd3cfe2a))
* **render:** add data-driven window classifier ([66079a8](https://github.com/Sawmonabo/sidekick-usages/commit/66079a85420f1b829424245e88d67a6b59d23e3c))
* **render:** add FetchFailure + render errors inside provider panels ([b01f456](https://github.com/Sawmonabo/sidekick-usages/commit/b01f456da17d9c2ef9674311c06872551c11ca44))
* **render:** add heat band + tile helpers ([cd47144](https://github.com/Sawmonabo/sidekick-usages/commit/cd4714487a4365049ab4ec9a69b4aaf359274343))
* **render:** add panel breathing room and caption separator ([444ca6e](https://github.com/Sawmonabo/sidekick-usages/commit/444ca6e105fb7ed6f95936b8bad2a0514fc45767))
* **render:** add provider-grouped heat-panel overview ([474627e](https://github.com/Sawmonabo/sidekick-usages/commit/474627ee12631c2c375cdd1100889f8adf2d1dde))
* **render:** content-width panels with full model-name captions ([c4a62ea](https://github.com/Sawmonabo/sidekick-usages/commit/c4a62ea1f8ba118597d80e7d1c1bf6b174c3789e))


### Fixed

* **lifetime:** guard non-dict JSON in stats/cache readers ([c291d43](https://github.com/Sawmonabo/sidekick-usages/commit/c291d4363c6fd2c4c7e2f33390734a929bff4ac0))
* **render:** harden reset formatters against tz-naive timestamps ([4e219cf](https://github.com/Sawmonabo/sidekick-usages/commit/4e219cff03e8ebc271ad4053aeb2f510cdae6f92))


### Docs

* add usage TUI redesign design spec (Framed Panels) ([c861169](https://github.com/Sawmonabo/sidekick-usages/commit/c861169e1ff27a7c4c4656db947df7496be1e30f))
* add usage TUI redesign implementation plan ([aaf8239](https://github.com/Sawmonabo/sidekick-usages/commit/aaf8239db0b8d6b7db19ebdd3f06e480e0c148fb))
* mark usage TUI spec approved; fold plan-detection fix into scope ([2aef6e7](https://github.com/Sawmonabo/sidekick-usages/commit/2aef6e76a52935dd2c285128db4d42e89414adda))
* **spec:** set the panel floor to 85 cols after approved breathing room ([c2919ad](https://github.com/Sawmonabo/sidekick-usages/commit/c2919ad63221aa10f5eac686a5144fdc853fecac))

## [0.5.0](https://github.com/Sawmonabo/sidekick-usages/compare/v0.4.1...v0.5.0) (2026-06-12)


### Added

* add usage window heartbeat ([0475acc](https://github.com/Sawmonabo/sidekick-usages/commit/0475acc14e6d1a560244a8b05fbfa7aa07753bea))

## [0.4.1](https://github.com/Sawmonabo/sidekick-usages/compare/v0.4.0...v0.4.1) (2026-06-12)


### Fixed

* **ci:** clean homebrew tag checkout before staging ([7f85adc](https://github.com/Sawmonabo/sidekick-usages/commit/7f85adc0c83f0d0fcbc675186c65d301892eb575))
* **ci:** wait for tap checks before watching ([eb88d17](https://github.com/Sawmonabo/sidekick-usages/commit/eb88d1797577b8d2051251c6fadf6ff6d81b5a1c))
* **homebrew:** derive formula resources from resolver ([0c2a733](https://github.com/Sawmonabo/sidekick-usages/commit/0c2a733e80304b40635ef5e2386532cbfd5c812d))

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

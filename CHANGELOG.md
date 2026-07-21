# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0](https://github.com/Sawmonabo/sidekick-usages/compare/v0.6.0...v0.7.0) (2026-07-21)


### Added

* **cli:** add provider hierarchy and deprecated aliases ([98de5fa](https://github.com/Sawmonabo/sidekick-usages/commit/98de5fa2b3f1c0a019de03cc7aabac79e9ef6292))
* **cli:** add shared robot branding ([42cd01e](https://github.com/Sawmonabo/sidekick-usages/commit/42cd01eb17c7903b385b1b4e259cf5b0c64126c5))
* **cli:** add short help alias and branded readme ([18fe3b8](https://github.com/Sawmonabo/sidekick-usages/commit/18fe3b8811337101b8a6ca68cb7c26f26101c4a1))
* **credentials:** harden Claude credential lifecycle ([cffb1a3](https://github.com/Sawmonabo/sidekick-usages/commit/cffb1a39a33dfb5bda5a1cae93a54837f260bcde))
* **daemon:** assess scheduler quiescence ([53b36ec](https://github.com/Sawmonabo/sidekick-usages/commit/53b36ec1a10b00bc278c09c6b45f9a171a80462f))
* **persistence:** classify recoverable account state ([499838c](https://github.com/Sawmonabo/sidekick-usages/commit/499838c504e75316edb797204f99618b208af823))
* **render:** show token activity start years ([15cef27](https://github.com/Sawmonabo/sidekick-usages/commit/15cef27bf91029f911d87597efca9e410b3a67fd))
* **usage:** persist authoritative token activity ([762be0e](https://github.com/Sawmonabo/sidekick-usages/commit/762be0e48aba5a37eb59e9cf4b24f764110473c5))


### Fixed

* **activity:** use authoritative provider token totals ([288b894](https://github.com/Sawmonabo/sidekick-usages/commit/288b894c08aa542f5db3de028b1b3e4de92b8962))
* **ci:** make architecture checks platform-native ([6d78c95](https://github.com/Sawmonabo/sidekick-usages/commit/6d78c95f5bc5c7ddbff0ddf935c6b10d3fb2cee0))
* **ci:** prove native package installation ([46874d2](https://github.com/Sawmonabo/sidekick-usages/commit/46874d2bb39fef6a02dd0f006c2409d9211920ef))
* **ci:** restore cross-platform release gates ([8a8c48d](https://github.com/Sawmonabo/sidekick-usages/commit/8a8c48d51bbb0d1e6a1d8abf50cd04658bfc2dc8))
* **ci:** use protected fixtures on Windows ([e2a2513](https://github.com/Sawmonabo/sidekick-usages/commit/e2a251311898329c52308032e33d668920a50411))
* **claude:** retry overloaded usage probes ([8a4ce70](https://github.com/Sawmonabo/sidekick-usages/commit/8a4ce700ecf294910a217880996470477d9fbcd7))
* **cli:** make daemon operations exhaustive ([073a051](https://github.com/Sawmonabo/sidekick-usages/commit/073a0514c41cba4d1532b7a3168e1ea1886242d7))
* **cli:** restore cross-platform quality gates ([729bb05](https://github.com/Sawmonabo/sidekick-usages/commit/729bb05e6fd25e278668b5c6a51cc4384aadcc44))
* **core:** preserve runtime boundary invariants ([a74985f](https://github.com/Sawmonabo/sidekick-usages/commit/a74985fa613423c6c7000482121161b97c79f139))
* **daemon:** retain logs on task removal ([d2f2f4c](https://github.com/Sawmonabo/sidekick-usages/commit/d2f2f4c8a74a515ef059222dada86cdc06e050b7))
* **heartbeat:** preserve explicit empty registries ([d4e8172](https://github.com/Sawmonabo/sidekick-usages/commit/d4e8172509b49cc16e0540f97fe97581ca4efde6))
* **help:** share terminal width policy ([96b7a52](https://github.com/Sawmonabo/sidekick-usages/commit/96b7a52ce107b6cfc4a0fdfc4a594ac89d63aee6))
* **lifetime:** preserve collection failure states ([d5e4f7d](https://github.com/Sawmonabo/sidekick-usages/commit/d5e4f7de811117a7f7aa27c30eaa7613ee2ee77a))
* **migration:** accept released Codex config endings ([ff441c4](https://github.com/Sawmonabo/sidekick-usages/commit/ff441c49700e8d0d5892f0643228a9d3a4ced71d))
* **persistence:** accept Windows default owner ([22e9b9e](https://github.com/Sawmonabo/sidekick-usages/commit/22e9b9e68c261876a529e66f25ed7c203c4fbea1))
* **persistence:** close native filesystem contracts ([c67968f](https://github.com/Sawmonabo/sidekick-usages/commit/c67968fee144c5c4099c81f2f2f89556ed96c6ee))
* **persistence:** close native platform qualification gaps ([daf5fcf](https://github.com/Sawmonabo/sidekick-usages/commit/daf5fcfcb9c54476a6390484ada253449985e39d))
* **persistence:** qualify native deletion and locking ([8912aae](https://github.com/Sawmonabo/sidekick-usages/commit/8912aae6e2356e5f1316b3bcc74483923330b265))
* **persistence:** qualify native macOS descriptors ([829c735](https://github.com/Sawmonabo/sidekick-usages/commit/829c735c2a65e246062927d5b8f4257f649657c8))
* **windows:** separate ACL access masks ([3c230d2](https://github.com/Sawmonabo/sidekick-usages/commit/3c230d28a582041615e11af5f0263de0ad6d2227))
* **windows:** trust owner-rights ACL entries ([54a239e](https://github.com/Sawmonabo/sidekick-usages/commit/54a239e98a9ed08e283e61356369832189ae9298))


### Changed

* **architecture:** harden persistence and usage boundaries ([56cc657](https://github.com/Sawmonabo/sidekick-usages/commit/56cc657052302e770547e25933205de216fc1893))
* **cli:** create command packages and lazy composition ([154d605](https://github.com/Sawmonabo/sidekick-usages/commit/154d605055f2730c3e44d1534e6ac57a572ece8a))
* **cli:** narrow command service boundaries ([7b7f102](https://github.com/Sawmonabo/sidekick-usages/commit/7b7f102414f07e123753e6e4adeb0af636f7002e))
* **core:** centralize proven shared types ([e63a4fa](https://github.com/Sawmonabo/sidekick-usages/commit/e63a4fa891c0ff75b1ce32f4a0e0507967e2a821))
* **core:** normalize models and expiry policy ([7594b8a](https://github.com/Sawmonabo/sidekick-usages/commit/7594b8a5e3d9f11c3f78e94998347b4bc76c4a6c))
* **credentials:** centralize credential state ([d8617b3](https://github.com/Sawmonabo/sidekick-usages/commit/d8617b3734d6622db9da5b3d581f922cb844ca8d))
* **http:** centralize transport and retry policy ([ba84a33](https://github.com/Sawmonabo/sidekick-usages/commit/ba84a330b4179da7586dc44449e42ed24f9a4e79))
* **paths:** inject current sidekick-owned paths ([21ee01c](https://github.com/Sawmonabo/sidekick-usages/commit/21ee01cb39b167653a1e225d4de12db35dbf7be2))
* **persistence:** coordinate credential transactions ([43b3121](https://github.com/Sawmonabo/sidekick-usages/commit/43b312194487309c1a4699d812097c9e9fff4e7d))
* **persistence:** validate versioned account schemas ([2588efe](https://github.com/Sawmonabo/sidekick-usages/commit/2588efe119ce8c5aa585f016cb83483a6f2d419d))
* **time:** inject explicit application wall time ([018d41c](https://github.com/Sawmonabo/sidekick-usages/commit/018d41c53537a44094033e7b66dfe6eef07582cb))


### Docs

* **activity:** document scoped provider totals ([469148b](https://github.com/Sawmonabo/sidekick-usages/commit/469148b2d9a27cfddd490ab495322436c02af851))
* **architecture:** add application design and plan ([73ce068](https://github.com/Sawmonabo/sidekick-usages/commit/73ce06891747a0571276b35c3f54c7de2c4e188f))
* **architecture:** approve HTTP and path dependencies ([90883d8](https://github.com/Sawmonabo/sidekick-usages/commit/90883d8b76a8142722a98633dc701593554d1a1b))
* **architecture:** close credential and migration contracts ([277b143](https://github.com/Sawmonabo/sidekick-usages/commit/277b1435bba3e76ee1dbcf52a33f2ed19648a108))
* **architecture:** close credential transaction contracts ([2a1f9c1](https://github.com/Sawmonabo/sidekick-usages/commit/2a1f9c1fd8cb54b9c55934293f52775df5758bee))
* **architecture:** document completed application migration ([8e3f241](https://github.com/Sawmonabo/sidekick-usages/commit/8e3f24118830d8360dab67f18541fb1de9ad14a9))
* **architecture:** record approval and execution evidence ([0a167e5](https://github.com/Sawmonabo/sidekick-usages/commit/0a167e59665a22a5a911bd16f5a73d07ddde304a))
* **architecture:** record cli package completion ([56fbd10](https://github.com/Sawmonabo/sidekick-usages/commit/56fbd1028b8afe27d4d6dce0d54e2edf06d6553c))
* **architecture:** record dependency decision provenance ([d5dc3f4](https://github.com/Sawmonabo/sidekick-usages/commit/d5dc3f49d7abc6f6a62f943d9b08ab696f418bfb))
* **architecture:** record persistence decision provenance ([986b4ae](https://github.com/Sawmonabo/sidekick-usages/commit/986b4ae1f3d787042b9dac24463c3b0b566194b7))
* **architecture:** record schema decision provenance ([51e1ab1](https://github.com/Sawmonabo/sidekick-usages/commit/51e1ab1301d75007edb914bee0811a8f68b2f0cd))
* **architecture:** record Windows CI follow-up ([0e318a6](https://github.com/Sawmonabo/sidekick-usages/commit/0e318a6ba2d6c094d8099dab4940ab7c2ac16374))
* **architecture:** specify typed cli composition ([5cc7f00](https://github.com/Sawmonabo/sidekick-usages/commit/5cc7f00f49d5f2de2635b2a30851e1b5a3c7b272))
* clean identities and markdown gates ([a97d2d4](https://github.com/Sawmonabo/sidekick-usages/commit/a97d2d44223641e5bdf42987704c7ef70574fd8a))
* **design:** add remote credential vault architecture ([e55c2c9](https://github.com/Sawmonabo/sidekick-usages/commit/e55c2c91e06ad9972eedec636d4383777713296c))
* **design:** consolidate remote vault architecture ([2921cb3](https://github.com/Sawmonabo/sidekick-usages/commit/2921cb35ad00623e8237c69833f6b9dbb56c6e0e))
* **design:** correct token activity architecture ([221a49c](https://github.com/Sawmonabo/sidekick-usages/commit/221a49ceb67b7d9a9b74e11a5ddcff2f7b4b31b0))
* organize and harden provider guidance ([cf3c366](https://github.com/Sawmonabo/sidekick-usages/commit/cf3c366c355aef54479e94f4f884e383ecf581eb))
* overhaul README and add contributor guide ([62a9cb1](https://github.com/Sawmonabo/sidekick-usages/commit/62a9cb163792787790350beb7da92abaea9bfcef))
* **persistence:** close implementation contract ([f8feda2](https://github.com/Sawmonabo/sidekick-usages/commit/f8feda2f87947030a5dfa4bb21889a81070f80a9))
* **persistence:** define released-writer race recovery ([478776f](https://github.com/Sawmonabo/sidekick-usages/commit/478776fe957aea64d0395d56b6adb7e7e9dbdccf))
* **persistence:** define schema migration recovery contract ([82f3893](https://github.com/Sawmonabo/sidekick-usages/commit/82f38939ffff964258afe18d2a90ba4b305db301))
* **persistence:** make native relocation explicit ([5cb456b](https://github.com/Sawmonabo/sidekick-usages/commit/5cb456b671f7dfa77677e47fa872406bc9694b08))
* **persistence:** record rollback compatibility preflight ([3a28c8b](https://github.com/Sawmonabo/sidekick-usages/commit/3a28c8bb6edca7f8cc6a3559cc481c853c44b621))
* **plan:** add token activity accuracy implementation plan ([8fd78f9](https://github.com/Sawmonabo/sidekick-usages/commit/8fd78f96a7542e32ea63fe09895c9e88eafb49fe))
* **plan:** record credential lifecycle publication ([790d73b](https://github.com/Sawmonabo/sidekick-usages/commit/790d73b300d184a4074d45967e6e99e3d0c172cb))
* refresh repository agent guidance ([7d7e91c](https://github.com/Sawmonabo/sidekick-usages/commit/7d7e91c590285ccb2eb08fd4e7378228ad7ce77c))
* **research:** decide application path discovery dependency ([601ee7c](https://github.com/Sawmonabo/sidekick-usages/commit/601ee7c88ce4907e31449cc2b570e5ffaf20cccd))
* **research:** decide HTTP transport and retry dependency ([9559d43](https://github.com/Sawmonabo/sidekick-usages/commit/9559d43b77c126b1adca158c000d87f7f36455ad))
* **research:** decide schema validation dependency ([986e1f7](https://github.com/Sawmonabo/sidekick-usages/commit/986e1f7d430ae3a1a057c37a2ae2a0c8fb5023de))
* **research:** fix retry source wording ([58d9b57](https://github.com/Sawmonabo/sidekick-usages/commit/58d9b571e5b490d60d560d1eeccafd62e9064d26))

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

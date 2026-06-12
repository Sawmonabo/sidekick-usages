# Homebrew packaging

The formula installed by end users lives in the separate
`Sawmonabo/homebrew-tap` repository under `Formula/sidekick-usages.rb`.
This directory keeps the generator and in-tree formula copy alongside
the Python source so release PRs can review both code and packaging
changes together.

## End-user install

Users install with:

```bash
brew tap Sawmonabo/tap
brew install sidekick-usages
```

## Source of truth

`packaging/homebrew/generate.py` is the source of truth for formula
content. Its output should match both:

- `packaging/homebrew/sidekick-usages.rb` in this repository
- `Formula/sidekick-usages.rb` in `Sawmonabo/homebrew-tap`

The generator reads the project version from `pyproject.toml`, resolves
runtime Python resources, hashes the GitHub tag archive, and emits the
complete Ruby formula.

## Release workflow

The `.github/workflows/bump-homebrew.yml` workflow regenerates the
formula after a successful `Publish to PyPI` run for a `v*` tag, then
opens pull requests for:

- this repository's in-tree formula copy
- the external `Sawmonabo/homebrew-tap` formula

For local verification or workflow reruns, generate the formula with:

```bash
uv run packaging/homebrew/generate.py --output /tmp/sidekick-usages.rb
diff /tmp/sidekick-usages.rb packaging/homebrew/sidekick-usages.rb
```

Then test the generated formula with Homebrew:

```bash
brew install --build-from-source /tmp/sidekick-usages.rb
brew test sidekick-usages
brew audit --strict sidekick-usages
brew uninstall sidekick-usages
```

## Tap setup

The workflow needs a `HOMEBREW_TAP_TOKEN` repository secret with write
access to `Sawmonabo/homebrew-tap` contents and pull requests. Keep the
token scoped to that tap repository only.

## Why a separate tap?

Homebrew core only accepts formulas with broad user bases and an
established release cadence. A personal tap (`Sawmonabo/tap`) keeps
release timing under project control without waiting for Homebrew core
review on every version bump.

If sidekick-usages crosses Homebrew's notability thresholds later, the
formula can move to `homebrew/core` and the personal tap can be retired.

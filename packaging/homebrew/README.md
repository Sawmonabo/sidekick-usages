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
generation. For each release, its output should match both:

- `packaging/homebrew/sidekick-usages.rb` in this repository
- `Formula/sidekick-usages.rb` in `Sawmonabo/homebrew-tap`

Between releases, those checked-in formulas intentionally remain tied to the
latest released tag while development dependencies can move ahead. The
generator reads the project version from `pyproject.toml`, constrains the
platform runtime closure to `uv.lock`, hashes the GitHub tag archive, and emits
the complete Ruby formula from source-distribution resources. It rejects a
missing source distribution or an unreviewed native source-build closure.

For unreleased development, the generated current-source formula is the
dependency-complete artifact. Its resource set follows the current locked host
runtime closure automatically, including transitive resources. Do not copy its
local source URL or archive hash into the checked-in release formula; that
formula changes only after a real release tag supplies the matching source
archive.

Pydantic-core 2.46.4 is the currently reviewed native resource. Its upstream
metadata requires maturin `>=1.10,<2` and Rust `>=1.88`; the generated formula
therefore declares both `maturin` and `rust` as build dependencies. A different
pydantic-core version or another native resource must be reviewed and encoded
before generation can proceed.

## Release workflow

The `.github/workflows/bump-homebrew.yml` workflow regenerates the
formula after a successful `Publish to PyPI` run for a `v*` tag, then
opens pull requests for:

- this repository's in-tree formula copy
- the external `Sawmonabo/homebrew-tap` formula

Current Homebrew requires formulae to live in a tap. For local verification or
workflow reruns from a checkout of the release tag, create an ephemeral no-git
tap and generate the release formula into its `Formula/` directory:

```bash
brew tap-new --no-git sidekick-usages/ci
FORMULA_PATH="$(brew --repository sidekick-usages/ci)/Formula/sidekick-usages.rb"
uv run packaging/homebrew/generate.py --output "$FORMULA_PATH"
diff "$FORMULA_PATH" packaging/homebrew/sidekick-usages.rb
```

Then test the generated formula with Homebrew:

```bash
brew install --build-from-source sidekick-usages/ci/sidekick-usages
brew test sidekick-usages/ci/sidekick-usages
brew audit --strict sidekick-usages/ci/sidekick-usages
brew uninstall sidekick-usages/ci/sidekick-usages
brew untap sidekick-usages/ci
```

CI tests unreleased source without rewriting the released formula template. It
creates a `git archive` of the checked-out commit and passes it explicitly:

```bash
brew tap-new --no-git sidekick-usages/ci
FORMULA_PATH="$(brew --repository sidekick-usages/ci)/Formula/sidekick-usages.rb"
git archive --format=tar.gz \
  --prefix=sidekick-usages-source/ \
  --output=/tmp/sidekick-usages-source.tar.gz HEAD
uv run packaging/homebrew/generate.py \
  --source-archive /tmp/sidekick-usages-source.tar.gz \
  --output "$FORMULA_PATH"
brew install --build-from-source sidekick-usages/ci/sidekick-usages
brew test sidekick-usages/ci/sidekick-usages
brew uninstall sidekick-usages/ci/sidekick-usages
brew untap sidekick-usages/ci
```

The checked-in formula remains tied to its released tag. Only the release
workflow regenerates that template against a new tag archive.

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

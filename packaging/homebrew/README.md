# Homebrew packaging

The `sidekick-usages.rb` formula in this directory is the source of
truth for the Homebrew tap. It lives **here** (alongside the source)
so PRs to the main repo can also bump the formula in a single
commit, but it must be **copied** into a separate tap repository
for end users to consume it.

## End-user install

Users install with:

```bash
brew tap Sawmonabo/tap
brew install sidekick-usages
```

This requires a separate GitHub repository named
`Sawmonabo/homebrew-tap` containing this `.rb` file under its
`Formula/` directory. Homebrew's naming convention is strict: tap
repos must start with `homebrew-`.

## Release workflow

For each new sidekick-usages release:

1. **Tag the release** in this repo: `git tag v0.2.0 && git push origin v0.2.0`.
   GitHub will auto-generate the source tarball at
   `https://github.com/Sawmonabo/sidekick-usages/archive/refs/tags/v0.2.0.tar.gz`.
2. **Compute the tarball SHA256:**
   ```bash
   curl -sL https://github.com/Sawmonabo/sidekick-usages/archive/refs/tags/v0.2.0.tar.gz \
     | shasum -a 256
   ```
3. **Update the formula:** replace the `url` version, `sha256`, and
   bump the `version` argument in the test block.
4. **Refresh Python dependency resources** if `pyproject.toml` changed:
   ```bash
   brew update-python-resources sidekick-usages.rb
   ```
   This auto-fills the bundled `resource` blocks with current PyPI
   tarball URLs and SHAs.
5. **Verify locally** before pushing:
   ```bash
   brew install --build-from-source ./sidekick-usages.rb
   brew test sidekick-usages
   brew audit --strict sidekick-usages
   brew uninstall sidekick-usages
   ```
6. **Copy the formula** to the tap repository:
   ```bash
   cp sidekick-usages.rb ~/dev/homebrew-tap/Formula/
   cd ~/dev/homebrew-tap
   git add Formula/sidekick-usages.rb
   git commit -m "sidekick-usages 0.2.0"
   git push
   ```

## Why a separate tap?

Homebrew core only accepts formulas with broad user bases and an
established release cadence. A personal tap (`Sawmonabo/tap`) keeps
us in full control of release timing and doesn't require a PR
review cycle from the Homebrew maintainers for each version bump.

If sidekick-usages ever crosses Homebrew's notability thresholds
(~75 GitHub stars, stable releases, third-party adoption), the
formula can be moved to `homebrew/core` and the tap retired.

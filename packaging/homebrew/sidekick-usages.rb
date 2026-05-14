# typed: false
# frozen_string_literal: true

# Sidekick-Usages formula.
#
# Lives in a separate tap repository (Sawmonabo/homebrew-tap) so
# users can install with:
#
#   brew tap Sawmonabo/tap
#   brew install sidekick-usages
#
# When cutting a new release:
#   1. Push a vX.Y.Z git tag to the sidekick-usages repo.
#   2. Run `brew bump-formula-pr` against this file, or update
#      url + sha256 + dependency pins manually.
#   3. Run `brew install --build-from-source ./sidekick-usages.rb`
#      locally to verify before pushing to the tap.
class SidekickUsages < Formula
  include Language::Python::Virtualenv

  desc "Check Claude Code and Codex CLI usage across multiple accounts"
  homepage "https://github.com/Sawmonabo/sidekick-usages"
  url "https://github.com/Sawmonabo/sidekick-usages/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_SHA256_OF_TAGGED_TARBALL_AT_RELEASE_TIME"
  license "Apache-2.0"
  head "https://github.com/Sawmonabo/sidekick-usages.git", branch: "main"

  depends_on "python@3.12"

  # Bundled wheels. Pin to exact versions matching pyproject.toml's
  # lower bounds; bump on each release via `brew update-python-resources`.
  resource "click" do
    url "https://files.pythonhosted.org/packages/source/c/click/click-8.1.7.tar.gz"
    sha256 "REPLACE_WITH_CLICK_SHA256"
  end

  resource "markdown-it-py" do
    url "https://files.pythonhosted.org/packages/source/m/markdown-it-py/markdown-it-py-3.0.0.tar.gz"
    sha256 "REPLACE_WITH_MDIT_SHA256"
  end

  resource "mdurl" do
    url "https://files.pythonhosted.org/packages/source/m/mdurl/mdurl-0.1.2.tar.gz"
    sha256 "REPLACE_WITH_MDURL_SHA256"
  end

  resource "pygments" do
    url "https://files.pythonhosted.org/packages/source/P/Pygments/pygments-2.18.0.tar.gz"
    sha256 "REPLACE_WITH_PYGMENTS_SHA256"
  end

  resource "rich" do
    url "https://files.pythonhosted.org/packages/source/r/rich/rich-13.9.4.tar.gz"
    sha256 "REPLACE_WITH_RICH_SHA256"
  end

  resource "shellingham" do
    url "https://files.pythonhosted.org/packages/source/s/shellingham/shellingham-1.5.4.tar.gz"
    sha256 "REPLACE_WITH_SHELLINGHAM_SHA256"
  end

  resource "typer" do
    url "https://files.pythonhosted.org/packages/source/t/typer/typer-0.15.1.tar.gz"
    sha256 "REPLACE_WITH_TYPER_SHA256"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/source/t/typing_extensions/typing_extensions-4.12.2.tar.gz"
    sha256 "REPLACE_WITH_TYPING_EXT_SHA256"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    # Verify the binary runs and reports its version.
    assert_match "sidekick-usages #{version}", shell_output("#{bin}/sidekick-usages --version")

    # Verify it exits cleanly when no accounts are saved
    # (exit code 1 is expected — there's nothing to show).
    output = shell_output("#{bin}/sidekick-usages list 2>&1", 0)
    assert_match(/no accounts saved/i, output)
  end
end

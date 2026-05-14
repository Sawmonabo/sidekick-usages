#!/usr/bin/env bash
#
# sidekick-usages bootstrap installer
# ----------------------------------
# Installs `uv` (if missing) and then `sidekick-usages` as a global
# tool. Idempotent — safe to re-run.
#
# Usage:
#   curl -LsSf https://raw.githubusercontent.com/Sawmonabo/sidekick-usages/main/install.sh | bash
#
# Or, from a local checkout:
#   ./install.sh
#
# Environment variables:
#   SIDEKICK_USAGES_VERSION   Pin to a specific version (e.g. 0.1.0)
#                            Defaults to latest on PyPI.
#   SIDEKICK_USAGES_NO_UV     If set, skip the uv install check.
#                            Use this when you already have uv or
#                            want to install with pipx instead.
#
set -euo pipefail

# ----------------------------------------------------------------------
# Pretty output helpers
# ----------------------------------------------------------------------
if [[ -t 1 ]]; then
    BOLD=$'\033[1m'
    DIM=$'\033[2m'
    RED=$'\033[31m'
    GREEN=$'\033[32m'
    YELLOW=$'\033[33m'
    CYAN=$'\033[36m'
    RESET=$'\033[0m'
else
    BOLD="" DIM="" RED="" GREEN="" YELLOW="" CYAN="" RESET=""
fi

info()  { printf "%s==>%s %s\n" "${CYAN}${BOLD}" "${RESET}" "$*"; }
ok()    { printf "%s✓%s %s\n" "${GREEN}" "${RESET}" "$*"; }
warn()  { printf "%s!%s %s\n" "${YELLOW}" "${RESET}" "$*" >&2; }
fail()  { printf "%s✗%s %s\n" "${RED}" "${RESET}" "$*" >&2; exit 1; }

# ----------------------------------------------------------------------
# Preflight checks
# ----------------------------------------------------------------------
info "sidekick-usages bootstrap installer"
echo

# Detect OS for friendlier messages later.
OS="$(uname -s)"
case "$OS" in
    Darwin|Linux) ;;
    MINGW*|MSYS*|CYGWIN*)
        fail "Native Windows isn't supported by this script. Use WSL2 or PowerShell + pipx."
        ;;
    *) warn "Unrecognized OS '$OS'. Proceeding anyway." ;;
esac

# Need curl or wget for uv install fallback.
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    fail "Neither curl nor wget found. Install one and re-run."
fi

# ----------------------------------------------------------------------
# Ensure uv is on PATH (unless user opted out)
# ----------------------------------------------------------------------
if [[ -n "${SIDEKICK_USAGES_NO_UV:-}" ]]; then
    info "SIDEKICK_USAGES_NO_UV set — skipping uv check."
    INSTALLER=""
elif command -v uv >/dev/null 2>&1; then
    ok "uv is already installed ($(uv --version))"
    INSTALLER="uv"
else
    info "uv not found. Installing from astral.sh..."
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    else
        wget -qO- https://astral.sh/uv/install.sh | sh
    fi

    # uv installs to ~/.local/bin; make sure it's findable in this
    # shell session, but warn that the user's PATH may need fixing.
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        fail "uv installation finished but 'uv' is still not on PATH. Open a new shell and re-run."
    fi
    ok "Installed uv $(uv --version | awk '{print $2}')"
    warn "If 'uv' isn't found in new shells, add this to your shell rc:"
    printf "  ${DIM}export PATH=\"\$HOME/.local/bin:\$PATH\"%s\n" "${RESET}"
    INSTALLER="uv"
fi

# ----------------------------------------------------------------------
# Install sidekick-usages
# ----------------------------------------------------------------------
VERSION_SPEC="sidekick-usages"
if [[ -n "${SIDEKICK_USAGES_VERSION:-}" ]]; then
    VERSION_SPEC="sidekick-usages==${SIDEKICK_USAGES_VERSION}"
    info "Installing $VERSION_SPEC..."
else
    info "Installing latest sidekick-usages from PyPI..."
fi

case "$INSTALLER" in
    uv)
        uv tool install --upgrade "$VERSION_SPEC"
        ;;
    "")
        # User asked us to skip uv. Try pipx as a fallback.
        if command -v pipx >/dev/null 2>&1; then
            pipx install --force "$VERSION_SPEC"
        else
            fail "SIDEKICK_USAGES_NO_UV is set but pipx isn't on PATH. Install pipx or unset SIDEKICK_USAGES_NO_UV."
        fi
        ;;
esac

# ----------------------------------------------------------------------
# Smoke test
# ----------------------------------------------------------------------
echo
if command -v sidekick-usages >/dev/null 2>&1; then
    INSTALLED_VERSION="$(sidekick-usages --version 2>&1 || true)"
    ok "${BOLD}${INSTALLED_VERSION}${RESET} is ready."
    echo
    echo "Next steps:"
    echo "  ${CYAN}sidekick-usages${RESET}                 # check usage for all accounts"
    echo "  ${CYAN}sidekick-usages add claude${RESET}      # save your first Claude Code account"
    echo "  ${CYAN}sidekick-usages --help${RESET}          # full command list"
else
    warn "Installed, but 'sidekick-usages' isn't on PATH in this shell."
    echo "Try opening a new terminal, or run:"
    echo "  ${DIM}export PATH=\"\$HOME/.local/bin:\$PATH\"${RESET}"
fi

#!/bin/sh
# free-code installer (macOS, Linux). Usage:
#   curl -fsSL https://free.ai/install.sh | sh
# Installs the freeai-code package from PyPI with uv (installed to the user dir
# if missing), else pipx, else a private venv in ~/.free-code. No sudo.
# Re-run to upgrade. Pin a version: FREE_CODE_VERSION=0.1.4 sh install.sh
# Force a method: FREE_CODE_INSTALLER=uv|pipx|venv
set -eu

PKG="freeai-code"
BIN="$HOME/.local/bin"
VENV="$HOME/.free-code/venv"

say() { printf '%s\n' "$*"; }
die() { printf 'free-code install failed: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

fetch() { # fetch URL DEST (HTTPS only)
  if have curl; then curl -fsSL --proto '=https' --tlsv1.2 "$1" -o "$2"
  elif have wget; then wget -q --https-only -O "$2" "$1"
  else return 1; fi
}

install_uv() {
  tmp=$(mktemp) || return 1
  if fetch "https://astral.sh/uv/install.sh" "$tmp"; then
    UV_INSTALL_DIR="$BIN" UV_NO_MODIFY_PATH=1 sh "$tmp" >&2 || { rm -f "$tmp"; return 1; }
    rm -f "$tmp"
  else
    rm -f "$tmp"; return 1
  fi
}

use_venv() {
  have python3 || die "python3 not found. Install Python 3.9+ (https://python.org/downloads/) and re-run."
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "python3 is older than 3.9."
  mkdir -p "$HOME/.free-code" "$BIN"
  python3 -m venv "$VENV" || die "python3 -m venv failed. On Debian/Ubuntu: apt install python3-venv, or install uv from https://docs.astral.sh/uv/"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip "$SPEC" || die "pip could not install $SPEC"
  ln -sf "$VENV/bin/free-code" "$BIN/free-code"
}

main() {
  SPEC="$PKG"
  [ -z "${FREE_CODE_VERSION:-}" ] || SPEC="$PKG==$FREE_CODE_VERSION"
  mkdir -p "$BIN"
  export UV_TOOL_BIN_DIR="$BIN" PIPX_BIN_DIR="$BIN"
  PATH="$BIN:$PATH"
  method="${FREE_CODE_INSTALLER:-}"

  if [ -z "$method" ]; then
    if have uv; then method=uv
    elif have pipx; then method=pipx
    elif install_uv && have uv; then method=uv
    else method=venv; fi
  fi

  say "Installing $SPEC with $method ..."
  case "$method" in
    uv)
      have uv || install_uv || die "could not install uv (needs curl or wget and access to astral.sh)."
      uv tool install --force "$SPEC" || die "uv could not install $SPEC" ;;
    pipx)
      have pipx || die "pipx not found."
      pipx install --force "$SPEC" || die "pipx could not install $SPEC" ;;
    venv) use_venv ;;
    *) die "unknown FREE_CODE_INSTALLER '$method' (use uv, pipx or venv)." ;;
  esac

  [ -x "$BIN/free-code" ] || die "free-code was not created in $BIN."
  say "Installed: $("$BIN/free-code" --version)"

  case ":${ORIG_PATH}:" in
    *":$BIN:"*) ;;
    *)
      case "${SHELL:-}" in
        */zsh) rc="~/.zshrc" ;;
        */bash) if [ "$(uname -s)" = Darwin ]; then rc="~/.bash_profile"; else rc="~/.bashrc"; fi ;;
        *) rc="~/.profile" ;;
      esac
      say ""
      say "$BIN is not on your PATH. Add it, then open a new terminal:"
      say "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> $rc" ;;
  esac
  say ""
  say "Next: run 'free-code' in a project folder. Docs: https://free.ai/code/"
}

ORIG_PATH="$PATH"
main "$@"

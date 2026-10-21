#!/usr/bin/env bash
# X5Agent one-liner setup (Linux / Mac).
# THE install/update command: installs + starts the console PERSISTENTLY in a
# single command through agent_boot.py (protected-file check first; detached:
# safe to close SSH; re-running updates and restarts on the same port 7777).
# First visit creates the account in the browser. Stop with `x5agent --off`.
#
# The repository is public, so no token is needed. A read-only token is still
# supported for private mirrors (never put it in your shell history):
#   X5_INSTALL_TOKEN=ghp_xxx bash setup.sh
# The token is used for one clone/pull and then removed from the git remote.
#
# With options (passed through to project/install.py):
#   X5_INSTALL_TOKEN=ghp_xxx bash setup.sh --email owner@mail.com --password S3cret123
# Re-running updates the source only (git pull --ff-only, data never touched).
set -euo pipefail

# Keep user site-packages out of the launcher and the installed service.
export PYTHONNOUSERSITE=1

REPO_SLUG="X5Coder/X5Agent"
REPO_URL="${X5_REPO_URL:-https://github.com/$REPO_SLUG.git}"
DIR="${X5_DIR:-$HOME/X5Agent}"
# Prefer the explicit install token, then the usual GitHub env names.
TOKEN="${X5_INSTALL_TOKEN:-${X5_GITHUB_TOKEN:-${GITHUB_TOKEN:-${GH_TOKEN:-}}}}"
if [ -n "$TOKEN" ]; then
  # x-access-token keeps the format valid for fine-grained and classic tokens.
  AUTH_URL="https://x-access-token:${TOKEN}@github.com/${REPO_SLUG}.git"
else
  AUTH_URL="$REPO_URL"
fi

# Never leave the token behind in .git/config: the remote always ends up clean.
clean_remote() {
  git -C "$DIR" remote set-url origin "$REPO_URL" >/dev/null 2>&1 || true
}
trap clean_remote EXIT

if ! command -v git >/dev/null 2>&1; then
  echo "[setup] ERROR: git is missing. Install it first: sudo apt install -y git" >&2
  exit 1
fi

if [ -d "$DIR/.git" ]; then
  echo "[setup] updating existing copy: $DIR"
  if [ -n "$TOKEN" ]; then
    git -C "$DIR" remote set-url origin "$AUTH_URL"
  fi
  git -C "$DIR" pull --ff-only || echo "[setup] WARN: git pull failed, continuing with current code"
  clean_remote
else
  echo "[setup] cloning into: $DIR"
  if ! git clone --depth 1 "$AUTH_URL" "$DIR"; then
    echo "[setup] ERROR: clone failed." >&2
    if [ -z "$TOKEN" ]; then
      echo "[setup] Check your network and that $REPO_SLUG is reachable." >&2
    else
      echo "[setup] Check that the token is valid and can read $REPO_SLUG." >&2
    fi
    exit 1
  fi
  # --depth 1 keeps the download small; unshallow so later pulls/updates work.
  git -C "$DIR" fetch --unshallow >/dev/null 2>&1 || true
  clean_remote
fi

PY=""
for cand in python3.12 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    PY="$cand"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "[setup] ERROR: no python found. Install python 3.12: sudo apt install -y python3.12" >&2
  exit 1
fi
echo "[setup] python: $($PY --version 2>&1)"

# Ubuntu's libpython3.*-minimal ships an apport sitecustomize hook. XAP/1
# refuses any loaded sitecustomize, so the console would show the paused page
# even though nothing was tampered with. Disable only the stock apport hook
# for THIS interpreter and keep it as *.x5-disabled (never delete it).
# X5_KEEP_SITECUSTOMIZE=1 opts out for administrators who need apport.
disable_stock_sitecustomize() {
  if [ "${X5_KEEP_SITECUSTOMIZE:-0}" = "1" ]; then
    echo "[setup] keeping sitecustomize (X5_KEEP_SITECUSTOMIZE=1)"
    return 0
  fi

  local pyver hook backup
  pyver="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  hook="/etc/python${pyver}/sitecustomize.py"

  if [ -f "$hook" ]; then
    if grep -q 'apport_python_hook' "$hook" 2>/dev/null; then
      backup="${hook}.x5-disabled"
      if [ -e "$backup" ]; then
        backup="${hook}.x5-disabled.$(date +%Y%m%d-%H%M%S)"
      fi
      if [ "$(id -u)" -eq 0 ]; then
        mv "$hook" "$backup" 2>/dev/null || echo "[setup] WARN: could not disable $hook" >&2
      elif command -v sudo >/dev/null 2>&1; then
        sudo mv "$hook" "$backup" 2>/dev/null || echo "[setup] WARN: could not disable $hook (sudo)" >&2
      else
        echo "[setup] WARN: $hook needs root to disable; run setup.sh with sudo." >&2
      fi
      [ -f "$hook" ] || echo "[setup] disabled stock Ubuntu sitecustomize: $hook -> $backup"
    else
      echo "[setup] WARN: $hook is not the stock apport hook; leaving it in place." >&2
    fi
  fi

  if ! "$PY" -c 'import sys; raise SystemExit(0 if "sitecustomize" not in sys.modules else 1)' >/dev/null 2>&1; then
    echo "[setup] WARN: a sitecustomize is still loaded; the console may show the paused page." >&2
  fi
}
disable_stock_sitecustomize

# Default to --run (install + start) unless the caller opts out with --no-run.
ARGS=("$@")
WANT_RUN=1
for a in "$@"; do
  if [ "$a" = "--no-run" ]; then
    WANT_RUN=0
    break
  fi
done

cd "$DIR/project"
# stdin fix (the classic curl|bash problem): when launched as
# `curl ... | bash`, this shell's stdin is the finished pipe (EOF). The normal
# flow creates the account in the browser, but explicit --email/--password
# prompts still need a real terminal, so recover it when one exists.
if [ -t 0 ]; then
  TTY_IN="/dev/stdin"    # already a terminal, keep it
elif [ -e /dev/tty ]; then
  TTY_IN="/dev/tty"      # pipe/file stdin but a terminal exists: use it
else
  TTY_IN="/dev/stdin"    # truly headless: flags/env must supply everything
fi
if [ "$WANT_RUN" = "1" ]; then
  # shellcheck disable=SC2094
  exec "$PY" install.py --run "${ARGS[@]}" < "$TTY_IN"
else
  FILTERED=()
  for a in "${ARGS[@]}"; do
    if [ "$a" != "--no-run" ]; then
      FILTERED+=("$a")
    fi
  done
  # shellcheck disable=SC2094
  exec "$PY" install.py "${FILTERED[@]:-}" < "$TTY_IN"
fi

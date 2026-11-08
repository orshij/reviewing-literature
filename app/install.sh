#!/bin/bash
# Litrev App: one-time setup on a teammate's Mac. Safe to run again: each step checks first and skips what is
# already done.   Usage: bash install.sh
# Optional environment:
#   LITREV_APP_SRC=<folder>     install the app from this checkout (development); skips the plugin
#   LITREV_MARKET_URL=<git url> the plugin marketplace to add (default: the GitHub repository orshij/reviewing-literature)
#   LITREV_INSTALL_DRY_RUN=1    run the checks and print the commands that would change something (tests use it)
#   LITREV_INSTALL_EXTRA_PATH   extra PATH entries (default /opt/homebrew/bin:/usr/local/bin)
set -u

MARKET_URL="${LITREV_MARKET_URL:-orshij/reviewing-literature}"
MARKET="litrev"
PLUGIN="reviewing-literature@$MARKET"
DRY="${LITREV_INSTALL_DRY_RUN:-}"
EXTRA_PATH="${LITREV_INSTALL_EXTRA_PATH-/opt/homebrew/bin:/usr/local/bin}"
export PATH="$HOME/.local/bin:$PATH${EXTRA_PATH:+:$EXTRA_PATH}"
export CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1   # only for a GitHub owner/repo LITREV_MARKET_URL: clone over HTTPS (Claude Code >= 2.1.141)

say()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '    ok: %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }
run()  { if [ -n "$DRY" ]; then printf '+ %s\n' "$*"; else "$@"; fi; }
runsh() { if [ -n "$DRY" ]; then printf '+ %s\n' "$1"; else /bin/bash -c "$1"; fi; }
have() { command -v "$1" >/dev/null 2>&1; }

auth_method() {
  env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_BASE_URL claude auth status --json 2>/dev/null \
    | plutil -extract authMethod raw -o - - 2>/dev/null
}
# Claude Code clones and updates the marketplace with the person's own git, never prompting: test it the same way.
# A GitHub owner/repo (what `claude plugin marketplace add` accepts) is checked over HTTPS, as Claude Code clones it.
git_url() { case "$1" in *://*|*@*:*|/*|.*) printf '%s' "$1" ;; */*) printf 'https://github.com/%s.git' "$1" ;; *) printf '%s' "$1" ;; esac; }
can_read_repo() { GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="ssh -o BatchMode=yes" git ls-remote "$(git_url "$MARKET_URL")" HEAD >/dev/null 2>&1; }
market_dir() {
  claude plugin marketplace list --json 2>/dev/null | /usr/bin/python3 -c '
import json, sys
for m in json.load(sys.stdin):
    if m.get("name") == sys.argv[1]:
        print(m.get("installLocation") or "")' "$MARKET" 2>/dev/null
}
plugin_installed() {
  claude plugin list --json 2>/dev/null | /usr/bin/python3 -c '
import json, sys
sys.exit(0 if any(p.get("id") == sys.argv[1] for p in json.load(sys.stdin)) else 1)' "$PLUGIN" 2>/dev/null
}

[ "$(uname -s)" = Darwin ] || die "This installer is for macOS."
printf 'Litrev App setup. The first run takes about 15 minutes and asks you to log in to Claude in your browser.\n'

say "1/9 Apple Command Line Tools (git)"
if xcode-select -p >/dev/null 2>&1; then ok "installed"
else
  run xcode-select --install
  note "A macOS window asks to install the Command Line Tools: click Install, then wait (5-15 minutes)."
  if [ -z "$DRY" ]; then until xcode-select -p >/dev/null 2>&1; do sleep 10; done; fi
  ok "installed"
fi

say "2/9 Claude Code"
if have claude; then ok "$(claude --version 2>/dev/null)"
else
  runsh "curl -fsSL https://claude.ai/install.sh | bash"     # official installer: code.claude.com/docs/en/setup
  [ -n "$DRY" ] || have claude || die "Claude Code did not install. See https://code.claude.com/docs/en/troubleshoot-install"
fi

say "3/9 Claude login"
if [ "$(auth_method)" = "claude.ai" ]; then ok "logged in with your Claude account"
else
  note "A browser page opens: log in with your Claude account (Team, Pro or Max) and confirm."
  run env -u ANTHROPIC_API_KEY claude auth login --claudeai
  [ -n "$DRY" ] || [ "$(auth_method)" = "claude.ai" ] || die "Claude Code is not logged in with a Claude account. Run this script again."
fi
[ -z "${ANTHROPIC_API_KEY:-}" ] || note "ANTHROPIC_API_KEY is set in your shell. The app removes it for its own sessions; in your own Claude Code sessions it overrides your Claude login."

APP_SRC="${LITREV_APP_SRC:-}"
if [ -n "$APP_SRC" ]; then
  say "4/9 and 5/9 plugin repository and plugin: skipped (installing the app from $APP_SRC)"
else
  say "4/9 Access to the plugin repository"
  if can_read_repo; then ok "git can read $MARKET_URL"
  else
    [ -n "$DRY" ] || die "git cannot read $MARKET_URL without a prompt. Check your internet connection (or set up git access to a private LITREV_MARKET_URL), then run this again."
  fi

  say "5/9 reviewing-literature plugin"
  if [ -n "$(market_dir)" ]; then ok "marketplace $MARKET is added"
  else run claude plugin marketplace add "$MARKET_URL" || die "Could not add the plugin marketplace (see the message above)."
  fi
  if [ -f "$HOME/.claude/skills/reviewing-literature/SKILL.md" ]; then
    ok "you have the skill in ~/.claude/skills; not installing the plugin, so the skill is not loaded twice"
  elif plugin_installed; then ok "plugin installed"
  else run claude plugin install "$PLUGIN" || die "Could not install the plugin (see the message above)."
  fi
  APP_SRC="$(market_dir)/app"
  [ "$APP_SRC" != "/app" ] || APP_SRC="<marketplace clone>/app"
fi

say "6/9 uv (installs the app and its Python)"
if have uv; then ok "$(uv --version)"
else
  runsh "curl -LsSf https://astral.sh/uv/install.sh | sh"     # official installer: docs.astral.sh/uv
  [ -n "$DRY" ] || have uv || die "uv did not install. See https://docs.astral.sh/uv/getting-started/installation/"
fi

say "7/9 Litrev App"
[ -n "$DRY" ] || [ -f "$APP_SRC/pyproject.toml" ] || die "The app is missing at $APP_SRC. Run this script again; if it happens again, open an issue at https://github.com/orshij/reviewing-literature/issues."
BIN="$(uv tool dir --bin 2>/dev/null)"; [ -n "$BIN" ] || BIN="$HOME/.local/bin"
LITREV_APP="$BIN/litrev-app"
WANT="$(sed -n 's/^version = "\(.*\)"/\1/p' "$APP_SRC/pyproject.toml" 2>/dev/null | head -1)"
HAVE="$(uv tool list 2>/dev/null | sed -n 's/^litrev-app v//p' | head -1)"
if [ -x "$LITREV_APP" ] && [ -z "${LITREV_APP_SRC:-}" ] && { [ -z "$WANT" ] || [ "$HAVE" = "$WANT" ]; }; then
  ok "installed at $LITREV_APP (update.sh updates it)"
elif [ -x "$LITREV_APP" ]; then      # an older copy (or a local checkout): replace it, or `litrev-app doctor` may not exist
  "$LITREV_APP" sessions >/dev/null 2>&1
  case $? in        # never rebuild the app's Python under a running review step or publish (M1c review)
    3) die "A review step or a publish is running in Litrev. Let it finish, or stop it in the app, then run this again." ;;
  esac
  run "$LITREV_APP" stop || true
  run uv tool install --force --reinstall --python 3.12 "$APP_SRC" || die "Could not update the app (see the message above)."
else run uv tool install --python 3.12 "$APP_SRC" || die "Could not install the app (see the message above)."
fi

say "8/9 pdftotext (optional: reads a paper's PDF when it has no web version)"
if have pdftotext; then ok "installed"
elif have brew; then run brew install poppler || note "Homebrew could not install poppler; the app works without it."
else note "skipped: Homebrew is not installed. Optional: install it from https://brew.sh, then run: brew install poppler"
fi

say "9/9 Launcher (~/Applications/Litrev.app)"
LAUNCHER_SRC="$APP_SRC/launcher/Litrev.applescript"
[ -n "$DRY" ] || [ -f "$LAUNCHER_SRC" ] || die "Launcher source missing: $LAUNCHER_SRC"
run mkdir -p "$HOME/Applications"
if [ -n "$DRY" ]; then printf '+ osacompile -o %s (litrevApp = %s)\n' "$HOME/Applications/Litrev.app" "$LITREV_APP"
else
  # osacompile ignores a source file given together with -e, so the property line and the source go in on stdin
  { printf 'property litrevApp : "%s"\n' "$LITREV_APP"; cat "$LAUNCHER_SRC"; } \
    | osacompile -o "$HOME/Applications/Litrev.app" || die "Could not build the launcher."
fi
ok "open Litrev from ~/Applications or Spotlight (type Litrev)"

say "Checking everything (litrev-app doctor)"
if [ -n "$DRY" ]; then printf '+ %s doctor\n' "$LITREV_APP"; exit 0; fi
if "$LITREV_APP" doctor; then
  printf '\nDone. To update later, run:  bash %s\nOpening Litrev...\n' "$APP_SRC/update.sh"
  open "$HOME/Applications/Litrev.app"
else
  printf '\nAlmost done: fix the items marked FAIL above, then run this script again (it skips what is done).\n'
  exit 1
fi

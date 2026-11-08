#!/bin/bash
# Litrev App: update the plugin and the app. Refuses while a review step or a publish window is running: a plugin
# update moves the skill folder (its path carries the version) out from under a running session.
#   Usage: bash <the plugin's marketplace clone>/app/update.sh    (install.sh prints the exact path)
# Optional environment: LITREV_APP_SRC, LITREV_INSTALL_DRY_RUN, LITREV_INSTALL_EXTRA_PATH (as in install.sh)
set -u

MARKET="litrev"
PLUGIN="reviewing-literature@$MARKET"
DRY="${LITREV_INSTALL_DRY_RUN:-}"
EXTRA_PATH="${LITREV_INSTALL_EXTRA_PATH-/opt/homebrew/bin:/usr/local/bin}"
export PATH="$HOME/.local/bin:$PATH${EXTRA_PATH:+:$EXTRA_PATH}"
export CLAUDE_CODE_PLUGIN_PREFER_HTTPS=1

say()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '    ok: %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\nSTOPPED: %s\n' "$*" >&2; exit 1; }
run()  { if [ -n "$DRY" ]; then printf '+ %s\n' "$*"; else "$@"; fi; }
have() { command -v "$1" >/dev/null 2>&1; }

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
check_idle() {
  "$LITREV_APP" sessions
  case $? in
    0) ok "nothing is running" ;;
    3) die "A review step or a publish is running (listed above). Let it finish, or stop it in the app, then run this again." ;;
    *) die "Could not check for running sessions." ;;
  esac
}

have claude || die "Claude Code is not installed: run install.sh."
have uv || die "uv is not installed: run install.sh."
BIN="$(uv tool dir --bin 2>/dev/null)"; [ -n "$BIN" ] || BIN="$HOME/.local/bin"
LITREV_APP="$BIN/litrev-app"
[ -x "$LITREV_APP" ] || die "The app is not installed: run install.sh."

say "Checking that nothing is running"
check_idle
say "Stopping the app (it starts with the new version the next time you open Litrev)"
run "$LITREV_APP" stop || note "it was not running"
check_idle   # nothing started in between

APP_SRC="${LITREV_APP_SRC:-}"
if [ -z "$APP_SRC" ]; then
  say "Updating the plugin"
  run claude plugin marketplace update "$MARKET" || die "Could not update the marketplace. Run install.sh to check your access to the plugin repository."
  if [ -f "$HOME/.claude/skills/reviewing-literature/SKILL.md" ]; then ok "the skill in ~/.claude/skills is used; plugin not touched"
  elif plugin_installed; then run claude plugin update "$PLUGIN" || die "Could not update the plugin."
  else run claude plugin install "$PLUGIN" || die "Could not install the plugin."
  fi
  APP_SRC="$(market_dir)/app"
fi

say "Updating the app"
[ -n "$DRY" ] || [ -f "$APP_SRC/pyproject.toml" ] || die "The app is missing at $APP_SRC."
# --reinstall: the version number may stay the same between updates, and uv would keep the old build
run uv tool install --force --reinstall --python 3.12 "$APP_SRC" || die "Could not install the app."

say "Checking everything (litrev-app doctor)"
if [ -n "$DRY" ]; then printf '+ %s doctor\n' "$LITREV_APP"; exit 0; fi
"$LITREV_APP" doctor; rc=$?
if [ $rc -eq 0 ]; then printf '\nUpdated. Open Litrev to continue.\n'; else printf '\nUpdated, but fix the items marked FAIL above.\n'; fi
exit $rc

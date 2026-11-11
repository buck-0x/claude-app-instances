#!/bin/zsh
#
# test-argless.sh: end-to-end tests for the argless-default rule. A main
# Claude process with no --user-data-dir runs on the default data folder, so
# `new-claude --reopen-all` and the default-folder launcher must treat it as
# that instance instead of starting a second copy. Runs against a stub app
# bundle so the real Claude.app is never touched; the default data folder is
# redirected with CLAUDE_LAUNCHER_DEFAULT_DATA_DIR.
#
# Usage:  tests/test-argless.sh          (takes ~1-2 minutes)

REPO_DIR="${0:A:h:h}"
NEW_CLAUDE="$REPO_DIR/bin/new-claude.sh"

TMP=$(mktemp -d /tmp/claude-argless-test.XXXXXX)
APPS="$TMP/Apps"
STUB_APP="$TMP/FakeClaude.app"
STUB_BIN="$STUB_APP/Contents/MacOS/stubclaude"
DEFAULT_NAME="Claude ArglessDefault"
OTHER_NAME="Claude ArglessOther"
LAUNCHER_DEFAULT="$APPS/$DEFAULT_NAME.app/Contents/MacOS/launcher"
LAUNCHER_OTHER="$APPS/$OTHER_NAME.app/Contents/MacOS/launcher"
OTHER_DATA_DIR="$HOME/Library/Application Support/claude-argless-test-other"
LOG_DIR="$HOME/Library/Logs/claude-instances"
DEFAULT_LOG="$LOG_DIR/$DEFAULT_NAME.log"

source "$REPO_DIR/tests/lib/stub-claude.sh"

export CLAUDE_LAUNCHER_POLL_SECS=1
export CLAUDE_LAUNCHER_GRACE_SECS=2
export CLAUDE_LAUNCHER_SETTLE_SECS=1
# Exit diagnostics must never read the real main.log or ShipIt cache.
export CLAUDE_LAUNCHER_CLAUDE_LOG="$TMP/main.log"
export CLAUDE_LAUNCHER_SHIPIT_DIR="$TMP/ShipIt"
mkdir -p "$CLAUDE_LAUNCHER_SHIPIT_DIR"
export CLAUDE_LAUNCHER_DEFAULT_DATA_DIR="$HOME/Library/Application Support/claude-argless-test-default"

log_has() { grep -qF -- "$1" "$DEFAULT_LOG" 2>/dev/null; }
stub_count() { stub_pids | wc -l | tr -d ' '; }

# Start an argless stub (only when no other stub runs; see the quirk note in
# tests/lib/stub-claude.sh) and wait until ps shows it.
start_argless() {
  local i
  "$STUB_BIN" &
  for i in {1..10}; do [ "$(argless_stub_count)" = "1" ] && return 0; sleep 0.5; done
  bad "precondition: argless stub is running"
}

# Stop both launchers' watchdogs and every stub, until nothing is left (a
# launcher opened through `open` may start a little late).
reset_all() {
  local i
  for i in {1..10}; do
    kill_watchdogs_for "$LAUNCHER_DEFAULT"
    kill_watchdogs_for "$LAUNCHER_OTHER"
    kill_stub
    sleep 1
    [ -z "$(stub_pids)$(watchdog_pids_for "$LAUNCHER_DEFAULT")$(watchdog_pids_for "$LAUNCHER_OTHER")" ] && break
  done
  rm -f "$DEFAULT_LOG"
}

cleanup() {
  reset_all
  rm -rf "$TMP"
  rm -f "$LOG_DIR/$DEFAULT_NAME.log" "$LOG_DIR/$DEFAULT_NAME.lock"
  rm -f "$LOG_DIR/$OTHER_NAME.log" "$LOG_DIR/$OTHER_NAME.lock"
}
trap cleanup EXIT INT TERM

# ---- build the stub "Claude" and two launchers ---------------------------------
build_stub_app
echo "Building launchers against stub app..."
"$NEW_CLAUDE" "$DEFAULT_NAME" --apps "$APPS" --claude-app "$STUB_APP" --dir claude-argless-test-default >/dev/null
"$NEW_CLAUDE" "$OTHER_NAME" --apps "$APPS" --claude-app "$STUB_APP" --dir claude-argless-test-other >/dev/null
[ -x "$LAUNCHER_DEFAULT" ] && [ -x "$LAUNCHER_OTHER" ] || { echo "!! launchers were not generated"; exit 1; }

echo ""
echo "Test 1: --reopen-all treats an argless stub as the running default instance"
start_argless
OUT=$("$NEW_CLAUDE" --reopen-all --apps "$APPS")
if print -r -- "$OUT" | grep -qF "$DEFAULT_NAME: already running"; then ok "default launcher reported already running"; else bad "default launcher reported already running"; fi
if print -r -- "$OUT" | grep -qF "$OTHER_NAME: reopening"; then ok "other launcher reopened"; else bad "other launcher reopened"; fi
sleep 3
if [ "$(argless_stub_count)" = "1" ] && ! stub_running_for "$CLAUDE_LAUNCHER_DEFAULT_DATA_DIR"; then
  ok "no second default instance started"
else
  bad "no second default instance started (argless=$(argless_stub_count))"
fi
reset_all

echo ""
echo "Test 2: the default launcher adopts an argless stub instead of launching a second one"
start_argless
"$LAUNCHER_DEFAULT" &
sleep 4
if log_has "adopting already-running instance"; then ok "launcher adopts the argless instance"; else bad "launcher adopts the argless instance"; fi
if [ "$(stub_count)" = "1" ]; then ok "exactly one stub running"; else bad "exactly one stub running (count=$(stub_count))"; fi
kill_stub
sleep 6
if [ -z "$(watchdog_pids_for "$LAUNCHER_DEFAULT")" ]; then ok "watchdog stands down after the argless instance quits"; else bad "watchdog stands down after the argless instance quits"; fi
reset_all

echo ""
echo "Test 3: a non-default launcher still ignores an argless stub"
start_argless
"$LAUNCHER_OTHER" &
sleep 4
if stub_running_for "$OTHER_DATA_DIR"; then ok "other launcher launched its own instance"; else bad "other launcher launched its own instance"; fi
if [ "$(stub_count)" = "2" ]; then ok "two stubs running"; else bad "two stubs running (count=$(stub_count))"; fi
reset_all

echo ""
echo "Test 4: a --type= helper line never counts as the argless instance"
"$STUB_BIN" --type=renderer &
sleep 1
OUT=$("$NEW_CLAUDE" --reopen-all --apps "$APPS")
if print -r -- "$OUT" | grep -qF "$DEFAULT_NAME: reopening"; then ok "default launcher reopened"; else bad "default launcher reopened"; fi
sleep 3
reset_all

echo ""
echo "Test 5: a non-flag first argument is not argless"
"$STUB_BIN" /tmp/claude-argless-script.js &
sleep 1
OUT=$("$NEW_CLAUDE" --reopen-all --apps "$APPS")
if print -r -- "$OUT" | grep -qF "$DEFAULT_NAME: reopening"; then ok "default launcher reopened"; else bad "default launcher reopened"; fi
sleep 3
reset_all

echo ""
echo "Test 6: ShipIt-style relaunch: the default instance comes back argless within the grace window"
# The default instance runs with --user-data-dir=<default test dir>. It is
# started directly and adopted (rather than launched through `open`, whose
# start-up time varies) so the timing below holds.
"$STUB_BIN" --user-data-dir="$CLAUDE_LAUNCHER_DEFAULT_DATA_DIR" &
sleep 1
"$LAUNCHER_DEFAULT" &
for i in {1..20}; do log_has "adopting already-running instance" && break; sleep 0.5; done
log_has "adopting already-running instance" || bad "precondition: default launcher adopted its instance"
kill_stub
# Longer than one poll (so the watchdog sees the instance gone), shorter than
# poll + grace (so the argless one is back before the grace check).
sleep 1.5
start_argless
sleep 4
if log_has "instance relaunched itself"; then
  ok "watchdog adopts the argless relaunch"
else
  bad "watchdog adopts the argless relaunch"
  sed 's/^/      /' "$DEFAULT_LOG" 2>/dev/null
fi
if [ "$(stub_count)" = "1" ]; then ok "no second instance launched"; else bad "no second instance launched (count=$(stub_count))"; fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]

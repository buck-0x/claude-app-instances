#!/bin/zsh
#
# test-watchdog.sh — end-to-end tests for the watchdog launcher, run against a
# stub app bundle so the real Claude.app is never touched. Uses short poll/grace
# intervals via the CLAUDE_LAUNCHER_*_SECS env hooks (they propagate because the
# tests exec the launcher directly instead of going through `open`).
#
# Usage:  tests/test-watchdog.sh          (takes ~1-2 minutes)

REPO_DIR="${0:A:h:h}"
NEW_CLAUDE="$REPO_DIR/bin/new-claude.sh"

TMP=$(mktemp -d /tmp/claude-watchdog-test.XXXXXX)
APPS="$TMP/Apps"
STUB_APP="$TMP/FakeClaude.app"
STUB_BIN="$STUB_APP/Contents/MacOS/stubclaude"
TEST_NAME="Claude WatchdogTest"
TEST_DIR_NAME="claude-watchdog-test-data"
TEST_DATA_DIR="$HOME/Library/Application Support/$TEST_DIR_NAME"
LAUNCHER="$APPS/$TEST_NAME.app/Contents/MacOS/launcher"
LOG_DIR="$HOME/Library/Logs/claude-instances"

export CLAUDE_LAUNCHER_POLL_SECS=1
export CLAUDE_LAUNCHER_GRACE_SECS=2
export CLAUDE_LAUNCHER_SETTLE_SECS=1

PASS=0
FAIL=0
ok()   { echo "  ✓ $1"; PASS=$((PASS+1)); }
bad()  { echo "  ✗ $1"; FAIL=$((FAIL+1)); }

# Search strings go through the environment, NOT awk -v: values on awk's own
# command line can match awk's own ps entry (racy self-match).
stub_running() {
  ps -axo command= | MB="$STUB_BIN" MD="--user-data-dir=$TEST_DATA_DIR" awk '
    BEGIN { b = ENVIRON["MB"]; d = ENVIRON["MD"] }
    index($0, b) && index($0, d) { f = 1 } END { exit f ? 0 : 1 }'
}
stub_pids()      { ps -axo pid=,command= | MB="$STUB_BIN" awk 'BEGIN { b = ENVIRON["MB"] } index($0, b) { print $1 }'; }
watchdog_pids()  { ps -axo pid=,command= | MS="$LAUNCHER" awk 'BEGIN { s = ENVIRON["MS"] } index($0, s) { print $1 }'; }
kill_stub()      { local p; p=$(stub_pids); [ -n "$p" ] && print -r -- "$p" | xargs kill 2>/dev/null; }
kill_watchdogs() { local p; p=$(watchdog_pids); [ -n "$p" ] && print -r -- "$p" | xargs kill 2>/dev/null; }

cleanup() {
  kill_watchdogs
  kill_stub
  sleep 1
  rm -rf "$TMP"
  rm -f "$LOG_DIR/$TEST_NAME.log" "$LOG_DIR/$TEST_NAME.lock"
}
trap cleanup EXIT INT TERM

# ---- build the stub "Claude" -------------------------------------------------
mkdir -p "$STUB_APP/Contents/MacOS" "$APPS"
cat > "$STUB_APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>FakeClaude</string>
    <key>CFBundleIdentifier</key>      <string>local.test.fakeclaude</string>
    <key>CFBundleVersion</key>         <string>100</string>
    <key>CFBundleShortVersionString</key><string>100</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleExecutable</key>      <string>stubclaude</string>
    <key>LSUIElement</key>             <true/>
</dict>
</plist>
PLIST
cat > "$STUB_BIN" <<'STUB'
#!/bin/zsh
# Emulate Electron's single-instance lock: if another stub is already running
# with the same arguments, hand off (exit) instead of running a second copy.
# (excluding our own not-yet-exec'd pipeline children, which briefly share
# this command line: skip rows whose pid or ppid is us; search strings go via
# ENVIRON so they never appear on awk's own command line)
if ps -axo pid=,ppid=,command= | MB="$0" MA="$*" MP=$$ awk '
    BEGIN { b = ENVIRON["MB"]; a = ENVIRON["MA"]; p = ENVIRON["MP"] }
    index($0, b) && index($0, a) && $1 != p && $2 != p { f = 1 } END { exit f ? 0 : 1 }'; then
  exit 0
fi
while :; do sleep 5; done
STUB
chmod +x "$STUB_BIN"
codesign --force --sign - "$STUB_APP" >/dev/null 2>&1 || true

# ---- build the launcher under test --------------------------------------------
echo "Building launcher against stub app..."
"$NEW_CLAUDE" "$TEST_NAME" --apps "$APPS" --claude-app "$STUB_APP" --dir "$TEST_DIR_NAME" >/dev/null
[ -x "$LAUNCHER" ] || { echo "!! launcher was not generated"; exit 1; }

echo ""
echo "Test 1: launcher starts the instance and stays resident"
"$LAUNCHER" &
WD1=$!
sleep 5
if stub_running;               then ok "stub instance is running"; else bad "stub instance is running"; fi
if kill -0 $WD1 2>/dev/null;   then ok "watchdog stays resident";  else bad "watchdog stays resident"; fi

echo ""
echo "Test 2: second launcher run defers to the live watchdog"
"$LAUNCHER" &
WD2=$!
for i in {1..20}; do kill -0 $WD2 2>/dev/null || break; sleep 0.5; done
if ! kill -0 $WD2 2>/dev/null; then ok "duplicate launcher exits quickly"; else bad "duplicate launcher exits quickly"; kill $WD2 2>/dev/null; fi
# Nothing in the duplicate-launcher path may spawn another instance; allow a
# settle window anyway so a regression shows as a stable extra process.
COUNT=""
for i in {1..20}; do
  COUNT="$(stub_pids | wc -l | tr -d ' ')"
  [ "$COUNT" = "1" ] && break
  sleep 0.5
done
if [ "$COUNT" = "1" ]; then
  ok "settles back to exactly one stub instance"
else
  bad "settles back to exactly one stub instance (count=$COUNT)"
  ps -axo pid=,ppid=,command= | grep -F "$STUB_BIN" | grep -vF grep | sed 's/^/      /'
fi

echo ""
echo "Test 3: user quit (no version change) stands the watchdog down"
kill_stub
sleep 8
if ! stub_running;               then ok "stub not relaunched";   else bad "stub not relaunched"; fi
if ! kill -0 $WD1 2>/dev/null;   then ok "watchdog exited";       else bad "watchdog exited"; kill $WD1 2>/dev/null; fi
if [ ! -e "$LOG_DIR/$TEST_NAME.lock" ]; then ok "lock released";  else bad "lock released"; fi

echo ""
echo "Test 4: update (version change) triggers a relaunch"
"$LAUNCHER" &
WD3=$!
sleep 5
stub_running || bad "precondition: stub running again"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion 101" "$STUB_APP/Contents/Info.plist"
kill_stub
sleep 10
if stub_running;               then ok "stub relaunched after version bump"; else bad "stub relaunched after version bump"; fi
if kill -0 $WD3 2>/dev/null;   then ok "watchdog still resident";            else bad "watchdog still resident"; fi

echo ""
echo "Test 5: second death on the same version is treated as a quit"
kill_stub
sleep 8
if ! stub_running;             then ok "stub not relaunched again"; else bad "stub not relaunched again"; fi
if ! kill -0 $WD3 2>/dev/null; then ok "watchdog exited";           else bad "watchdog exited"; kill $WD3 2>/dev/null; fi

LOG="$LOG_DIR/$TEST_NAME.log"
STUB_VER() { /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $1" "$STUB_APP/Contents/Info.plist"; }
# Stage a fake Squirrel update the way ShipIt does: a JSON ShipItState.plist
# pointing at an unpacked bundle; ShipIt deletes that bundle once installed.
stage_update() {
  mkdir -p "$SHIPIT/update.T1/FakeClaude.app/Contents"
  cp "$STUB_APP/Contents/Info.plist" "$SHIPIT/update.T1/FakeClaude.app/Contents/Info.plist"
  /usr/libexec/PlistBuddy -c "Set :CFBundleVersion $1" "$SHIPIT/update.T1/FakeClaude.app/Contents/Info.plist"
  local url="file://$SHIPIT/update.T1/FakeClaude.app/"
  print -r -- "{\"launchAfterInstallation\":true,\"updateBundleURL\":\"${url//\//\\/}\",\"bundleIdentifier\":\"local.test.fakeclaude\"}" > "$SHIPIT/ShipItState.plist"
}
unstage_update() { rm -rf "$SHIPIT/update.T1"; }
log_has() { grep -qF -- "$1" "$LOG" 2>/dev/null; }

export CLAUDE_LAUNCHER_SHIPIT_DIR="$TMP/ShipIt"
export CLAUDE_LAUNCHER_CLAUDE_LOG="$TMP/main.log"
SHIPIT="$CLAUDE_LAUNCHER_SHIPIT_DIR"
mkdir -p "$SHIPIT"

echo ""
echo "Test 5a: quit to install a pending update waits, then reopens after install"
"$LAUNCHER" &
WD4=$!
sleep 4
stub_running || bad "precondition: stub running"
stage_update 102
sleep 3
if log_has "update 102 downloaded"; then ok "logs the staged update while running"; else bad "logs the staged update while running"; fi
print -r -- "$(date '+%Y-%m-%d %H:%M:%S') [info] [stealth-relaunch] otherAppFullScreen=false" >> "$CLAUDE_LAUNCHER_CLAUDE_LOG"
kill_stub
sleep 8
if ! stub_running;             then ok "not reopened while update is still pending"; else bad "not reopened while update is still pending"; fi
if kill -0 $WD4 2>/dev/null;   then ok "watchdog waits instead of standing down";   else bad "watchdog waits instead of standing down"; fi
if log_has "update pending";   then ok "logs the pending-update wait";               else bad "logs the pending-update wait"; fi
if log_has "claude log: " && log_has "stealth-relaunch"; then ok "logs Claude's own quit lines"; else bad "logs Claude's own quit lines"; fi
if log_has "context:";         then ok "logs exit context";                          else bad "logs exit context"; fi
STUB_VER 102; unstage_update
sleep 6
if stub_running;               then ok "reopened once the update installed"; else bad "reopened once the update installed"; fi
if log_has "update installed (101 -> 102)"; then ok "logs the install"; else bad "logs the install"; fi

echo ""
echo "Test 5b: pending update that vanishes without installing still reopens"
stage_update 103
kill_stub
sleep 8
if ! stub_running; then ok "waits while pending"; else bad "waits while pending"; fi
unstage_update
sleep 5
if stub_running; then ok "reopened after the pending update vanished"; else bad "reopened after the pending update vanished"; fi

echo ""
echo "Test 5c: opening the launcher during an update wait reopens immediately"
stage_update 104
kill_stub
sleep 7
stub_running && bad "precondition: still waiting"
"$LAUNCHER" &
sleep 5
if stub_running; then ok "reopened on launcher click"; else bad "reopened on launcher click"; fi
if log_has "to reopen it" && log_has "at launcher request"; then ok "reopened via the owning watchdog"; else bad "reopened via the owning watchdog"; fi
unstage_update

echo ""
echo "Test 5d: quit then reopen within the grace window reopens (no update)"
kill_stub
sleep 1.5
"$LAUNCHER" &
sleep 6
if stub_running;             then ok "quick reopen is honored"; else bad "quick reopen is honored"; fi
if kill -0 $WD4 2>/dev/null; then ok "original watchdog keeps watching"; else bad "original watchdog keeps watching"; fi
kill_stub
sleep 6
if ! kill -0 $WD4 2>/dev/null; then ok "plain quit still stands down"; else bad "plain quit still stands down"; kill $WD4 2>/dev/null; fi
if log_has "no pending update; assuming intentional quit"; then ok "logs the quit reason"; else bad "logs the quit reason"; fi
unset CLAUDE_LAUNCHER_SHIPIT_DIR CLAUDE_LAUNCHER_CLAUDE_LOG

echo ""
echo "Test 6: --reopen-all reopens a dead instance (default timings, via open)"
"$NEW_CLAUDE" --reopen-all --apps "$APPS" | grep -q "reopening" \
  && ok "reopen-all reports reopening" || bad "reopen-all reports reopening"
sleep 12
if stub_running; then ok "stub reopened by reopen-all"; else bad "stub reopened by reopen-all"; fi
if "$NEW_CLAUDE" --reopen-all --apps "$APPS" | grep -q "already running"; then
  ok "reopen-all sees it as already running"
else
  bad "reopen-all sees it as already running"
fi

echo ""
echo "Test 7: old-format (one-shot) launchers still parse for --list/--reopen-all"
OLD_APP="$APPS/Claude OldFormat.app"
mkdir -p "$OLD_APP/Contents/MacOS"
cat > "$OLD_APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDisplayName</key>     <string>Claude OldFormat</string>
    <key>CFBundleIdentifier</key>      <string>local.launcher.claude-oldformat</string>
    <key>CFBundleExecutable</key>      <string>launcher</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
</dict>
</plist>
PLIST
cat > "$OLD_APP/Contents/MacOS/launcher" <<OLD
#!/bin/zsh
exec open -n -a "/Applications/Claude.app" --args --user-data-dir="$HOME/Library/Application Support/Claude-OldFormat"
OLD
chmod +x "$OLD_APP/Contents/MacOS/launcher"
if "$NEW_CLAUDE" --list --apps "$APPS" | grep -q "Claude-OldFormat"; then
  ok "--list extracts data dir from old launcher"
else
  bad "--list extracts data dir from old launcher"
fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]

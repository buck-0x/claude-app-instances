# stub-claude.sh: shared helpers for zsh tests that run against a stub Claude
# app bundle, so the real Claude.app is never touched.
#
# Source it after setting TMP, APPS, STUB_APP and STUB_BIN.
# Search strings go through the environment, NOT awk -v: values on awk's own
# command line can match awk's own ps entry (racy self-match).

PASS=0
FAIL=0
ok()  { echo "  ✓ $1"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }

# Build the stub bundle (same bundle and Info.plist as tests/test-watchdog.sh).
#
# Known quirk: the stub's single-instance check matches its own arguments with
# awk `index($0, ENVIRON["MA"])`, and macOS awk returns 1 for `index(s, "")`,
# so an argless stub (empty MA) exits at once whenever any other stub is
# already running. Start an argless stub only when no other stub runs.
build_stub_app() {
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
}

# Is a stub running with --user-data-dir=$1 exactly (boundary-checked)?
stub_running_for() {
  ps -axo command= | MB="$STUB_BIN" MD="--user-data-dir=$1" awk '
    BEGIN { b = ENVIRON["MB"]; d = ENVIRON["MD"] }
    index($0, b) && (i = index($0, d)) {
      r = substr($0, i + length(d), 1)
      if (r == "" || r == " ") { f = 1; exit }
    }
    END { exit f ? 0 : 1 }'
}

# Pids of every running stub.
stub_pids() {
  ps -axo pid=,command= | MB="$STUB_BIN" awk 'BEGIN { b = ENVIRON["MB"] } index($0, b) { print $1 }'
}

# Stubs with no arguments or flags only, no --user-data-dir= and no --type=
# (the same argless rule new-claude and the launcher use).
argless_stub_count() {
  ps -axo pid=,command= | MB="$STUB_BIN" awk '
    BEGIN { b = ENVIRON["MB"] }
    (j = index($0, b)) && (j == 1 || substr($0, j - 1, 1) == " ") && !index($0, "--user-data-dir=") && !index($0, "--type=") {
      a = substr($0, j + length(b), 2)
      if (a == "" || a == " -") n++
    }
    END { print n + 0 }'
}

kill_stub() { local p; p=$(stub_pids); [ -n "$p" ] && print -r -- "$p" | xargs kill 2>/dev/null; }

# Pids of resident watchdogs running launcher script $1.
watchdog_pids_for() {
  ps -axo pid=,command= | MS="$1" awk 'BEGIN { s = ENVIRON["MS"] } index($0, s) { print $1 }'
}
kill_watchdogs_for() { local p; p=$(watchdog_pids_for "$1"); [ -n "$p" ] && print -r -- "$p" | xargs kill 2>/dev/null; }

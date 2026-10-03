#!/bin/zsh
#
# new-claude.sh — create a launcher app for a second, isolated Claude instance.
#
# Each launcher opens the REAL, unmodified Claude.app with its own
# --user-data-dir, so it gets a separate login, history, MCP servers, and
# Cowork environment. Claude's own bundle is never touched or re-signed —
# which is what avoids the Electron startup crash (EXC_BREAKPOINT in
# ElectronMain) that bundle-duplication hits on recent Claude builds.
#
# The launcher stays resident as a tiny watchdog: when a Claude auto-update
# quits the instance, it reopens it once the update has installed; when you
# quit the instance yourself (no update involved), it stands down.
#
# Usage:
#   new-claude "Claude Fulcra"
#   new-claude "Claude Fulcra" --dir "Claude-Fulcra"    # custom data folder
#   new-claude "Claude Fulcra" --apps ~/Applications     # custom install dir
#   new-claude --list                                    # list launchers created
#   new-claude --reopen-all                              # reopen instances that aren't running
#
# Re-running with the same name safely rebuilds that launcher.

set -e

# ---- config ------------------------------------------------------------------
CLAUDE_APP="/Applications/Claude.app"
SUPPORT_DIR="$HOME/Library/Application Support"

# ---- parse arguments ---------------------------------------------------------
DISPLAY_NAME=""
DATA_DIR_NAME=""
APPS_DIR="/Applications"
DO_LIST=0
DO_REOPEN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --list)       DO_LIST=1; shift ;;
    --reopen-all) DO_REOPEN=1; shift ;;
    --dir)        DATA_DIR_NAME="$2"; shift 2 ;;
    --apps)       APPS_DIR="${2/#\~/$HOME}"; shift 2 ;;
    --claude-app) CLAUDE_APP="${2/#\~/$HOME}"; shift 2 ;;  # test hook
    -h|--help)
      sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*)     echo "Unknown option: $1" >&2; exit 1 ;;
    *)      if [ -z "$DISPLAY_NAME" ]; then DISPLAY_NAME="$1"; fi; shift ;;
  esac
done

# ---- launcher inspection helpers ---------------------------------------------
# Data dir of a launcher app. New (watchdog) launchers carry a DATA_DIR= line;
# old one-shot launchers only have it inline in the open command.
launcher_data_dir() {
  local script="$1/Contents/MacOS/launcher" d=""
  d=$(sed -n 's/^DATA_DIR="\(.*\)"$/\1/p' "$script" 2>/dev/null | head -n1)
  if [ -z "$d" ]; then
    d=$(sed -n 's/.*--user-data-dir="\([^"]*\)".*/\1/p' "$script" 2>/dev/null | head -n1)
  fi
  print -r -- "$d"
}

# Claude app bundle a launcher points at (supports the --claude-app test hook).
launcher_claude_app() {
  local script="$1/Contents/MacOS/launcher" a=""
  a=$(sed -n 's/^CLAUDE_APP="\(.*\)"$/\1/p' "$script" 2>/dev/null | head -n1)
  if [ -z "$a" ]; then
    a=$(sed -n 's/.*open -n -a "\([^"]*\)".*/\1/p' "$script" 2>/dev/null | head -n1)
  fi
  print -r -- "$a"
}

# Is an instance of $1 (app bundle) running with --user-data-dir=$2 exactly?
# Search strings go through the environment, NOT awk -v: values on awk's own
# command line can match awk's own ps entry (racy self-match).
instance_running_for() {
  local capp="$1" datadir="$2" exe=""
  exe=$(/usr/libexec/PlistBuddy -c "Print :CFBundleExecutable" "$capp/Contents/Info.plist" 2>/dev/null) || exe=""
  ps -axo command= | MB="$capp/Contents/MacOS/${exe:-Claude}" MD="--user-data-dir=$datadir" awk '
    BEGIN { b = ENVIRON["MB"]; d = ENVIRON["MD"] }
    index($0, b) && (i = index($0, d)) {
      r = substr($0, i + length(d), 1)
      if (r == "" || r == " ") { f = 1; exit }
    }
    END { exit f ? 0 : 1 }'
}

# Kill any resident watchdog process for a launcher app path (never the
# Claude instance itself; its command line does not contain the launcher path).
kill_watchdogs_for() {
  local script="$1/Contents/MacOS/launcher" pids=""
  pids=$(ps -axo pid=,command= | MS="$script" awk 'BEGIN { s = ENVIRON["MS"] } index($0, s) { print $1 }') || true
  if [ -n "$pids" ]; then
    echo "Stopping resident watchdog for $1"
    print -r -- "$pids" | xargs kill 2>/dev/null || true
  fi
}

# ---- --list mode -------------------------------------------------------------
if [ "$DO_LIST" -eq 1 ]; then
  echo "Claude instance launchers found:"
  found=0
  for plist in "$APPS_DIR"/*.app/Contents/Info.plist(N); do
    [ -f "$plist" ] || continue
    id=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$plist" 2>/dev/null || true)
    case "$id" in
      local.launcher.*)
        app="${plist%/Contents/Info.plist}"
        name=$(/usr/libexec/PlistBuddy -c "Print :CFBundleDisplayName" "$plist" 2>/dev/null)
        datadir=$(launcher_data_dir "$app")
        echo "   • $name"
        echo "       app:  $app"
        echo "       data: $datadir"
        found=1 ;;
    esac
  done
  if [ "$found" -eq 0 ]; then
    echo "   (none yet — create one with: new-claude \"Claude Name\")"
  fi
  exit 0
fi

# ---- --reopen-all mode -------------------------------------------------------
if [ "$DO_REOPEN" -eq 1 ]; then
  echo "Checking Claude instance launchers:"
  found=0
  for plist in "$APPS_DIR"/*.app/Contents/Info.plist(N); do
    [ -f "$plist" ] || continue
    id=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$plist" 2>/dev/null || true)
    case "$id" in
      local.launcher.*)
        found=1
        app="${plist%/Contents/Info.plist}"
        name=$(/usr/libexec/PlistBuddy -c "Print :CFBundleDisplayName" "$plist" 2>/dev/null)
        datadir=$(launcher_data_dir "$app")
        capp=$(launcher_claude_app "$app")
        [ -n "$capp" ] || capp="$CLAUDE_APP"
        if [ -z "$datadir" ]; then
          echo "   • $name: could not determine data dir; skipping"
          continue
        fi
        if instance_running_for "$capp" "$datadir"; then
          echo "   • $name: already running"
        else
          echo "   • $name: reopening"
          # -n: launch even if LaunchServices holds a stale "running" record
          # for a recently killed watchdog; the launcher's own lock makes
          # duplicate starts harmless.
          open -n "$app"
        fi ;;
    esac
  done
  if [ "$found" -eq 0 ]; then
    echo "   (no launchers found)"
  fi
  exit 0
fi

if [ -z "$DISPLAY_NAME" ]; then
  echo "Usage: new-claude \"Claude Name\" [--dir DataFolderName] [--apps /install/path]" >&2
  echo "       new-claude --list | --reopen-all" >&2
  exit 1
fi

# ---- derive names ------------------------------------------------------------
# Default data folder: display name with spaces -> hyphens.
#   "Claude Fulcra" -> "Claude-Fulcra"
if [ -z "$DATA_DIR_NAME" ]; then
  DATA_DIR_NAME="${DISPLAY_NAME// /-}"
fi

DATA_DIR="$SUPPORT_DIR/$DATA_DIR_NAME"
APP_PATH="$APPS_DIR/$DISPLAY_NAME.app"

# ---- sanity checks -----------------------------------------------------------
if [ ! -d "$CLAUDE_APP" ]; then
  echo "!! $CLAUDE_APP not found. Install Claude first from https://claude.ai/download" >&2
  exit 1
fi

echo "Creating launcher:"
echo "   Name : $DISPLAY_NAME"
echo "   App  : $APP_PATH"
echo "   Data : $DATA_DIR"
echo ""

# ---- build the .app bundle ---------------------------------------------------
# Same structure Automator produces for an "Application", built directly so no
# GUI is needed. The app is a resident watchdog: it launches Claude with this
# instance's data dir and reopens it if an auto-update quits it.

# A watchdog from a previous build of this launcher must not keep running with
# the old script (it could double up with the new one).
kill_watchdogs_for "$APP_PATH"

rm -rf "$APP_PATH"
mkdir -p "$APP_PATH/Contents/MacOS"
mkdir -p "$APP_PATH/Contents/Resources"

cat > "$APP_PATH/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>$DISPLAY_NAME</string>
    <key>CFBundleDisplayName</key>     <string>$DISPLAY_NAME</string>
    <key>CFBundleIdentifier</key>      <string>local.launcher.${DATA_DIR_NAME:l}</string>
    <key>CFBundleVersion</key>         <string>2.0</string>
    <key>CFBundleShortVersionString</key><string>2.0</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleExecutable</key>      <string>launcher</string>
    <key>CFBundleIconFile</key>        <string>AppIcon</string>
    <key>LSUIElement</key>             <true/>
    <key>NSHighResolutionCapable</key> <true/>
</dict>
</plist>
PLIST

# The launcher runs the REAL Claude with its own data dir (signature stays
# intact), then stays resident as a watchdog. Header: instance values baked in.
cat > "$APP_PATH/Contents/MacOS/launcher" <<HDR
#!/bin/zsh
# Generated by new-claude. Launches Claude with this instance's data dir and
# babysits it: reopens the instance when a Claude auto-update quits it (after
# the update installs); stands down when you quit it (no update involved).
CLAUDE_APP="$CLAUDE_APP"
DATA_DIR="$DATA_DIR"
INSTANCE_NAME="$DISPLAY_NAME"
HDR

# Body: no expansion (runtime logic, appended verbatim).
cat >> "$APP_PATH/Contents/MacOS/launcher" <<'BODY'

POLL=${CLAUDE_LAUNCHER_POLL_SECS:-20}
GRACE=${CLAUDE_LAUNCHER_GRACE_SECS:-20}
SETTLE=${CLAUDE_LAUNCHER_SETTLE_SECS:-5}
# Longest wait for a pending update to install before reopening anyway, and
# how often to note that we're still waiting.
UPDATE_WAIT_MAX=${CLAUDE_LAUNCHER_UPDATE_WAIT_SECS:-14400}
HEARTBEAT=${CLAUDE_LAUNCHER_HEARTBEAT_SECS:-1800}
# Claude's own main log (shared by every instance) and Squirrel's ShipIt cache.
CLAUDE_LOG=${CLAUDE_LAUNCHER_CLAUDE_LOG:-$HOME/Library/Logs/Claude/main.log}
if [ -n "$CLAUDE_LAUNCHER_SHIPIT_DIR" ]; then
  SHIPIT_DIR="$CLAUDE_LAUNCHER_SHIPIT_DIR"
else
  BUNDLE_ID=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$CLAUDE_APP/Contents/Info.plist" 2>/dev/null) || BUNDLE_ID=""
  SHIPIT_DIR="$HOME/Library/Caches/${BUNDLE_ID:-com.anthropic.claudefordesktop}.ShipIt"
fi
LOG_DIR="$HOME/Library/Logs/claude-instances"
LOG_FILE="$LOG_DIR/$INSTANCE_NAME.log"
LOCK="$LOG_DIR/$INSTANCE_NAME.lock"
STAMP="$LOG_DIR/$INSTANCE_NAME.stamp"   # mtime = when we last launched/adopted
REOPEN_REQUESTED=0
inst_pid=""
started_at=$(date +%s)

mkdir -p "$LOG_DIR"

log() {
  if [ -f "$LOG_FILE" ] && [ "$(stat -f %z "$LOG_FILE" 2>/dev/null || echo 0)" -gt 524288 ]; then
    tail -c 65536 "$LOG_FILE" > "$LOG_FILE.tmp" 2>/dev/null && mv "$LOG_FILE.tmp" "$LOG_FILE"
  fi
  print -r -- "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG_FILE"
}

bundle_version() {
  /usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$CLAUDE_APP/Contents/Info.plist" 2>/dev/null || echo unknown
}

claude_binary() {
  local exe
  exe=$(/usr/libexec/PlistBuddy -c "Print :CFBundleExecutable" "$CLAUDE_APP/Contents/Info.plist" 2>/dev/null) || exe=""
  print -r -- "$CLAUDE_APP/Contents/MacOS/${exe:-Claude}"
}

# Exact match on the main binary AND our exact --user-data-dir (boundary-checked
# so ".../Claude-V1" never matches ".../Claude-V12"). Search strings go through
# the environment, NOT awk -v: values on awk's own command line can match awk's
# own ps entry (racy self-match).
instance_pid() {
  ps -axo pid=,command= | MB="$(claude_binary)" MD="--user-data-dir=$DATA_DIR" awk '
    BEGIN { b = ENVIRON["MB"]; d = ENVIRON["MD"] }
    index($0, b) && (i = index($0, d)) {
      r = substr($0, i + length(d), 1)
      if (r == "" || r == " ") { print $1; f = 1; exit }
    }
    END { exit f ? 0 : 1 }'
}

# Also records the pid so exit diagnostics can name it.
instance_running() {
  local p
  p=$(instance_pid) || return 1
  inst_pid="$p"
}

# Mark the start of a new instance lifetime (for crash-report lookup + uptime).
mark_started() {
  started_at=$(date +%s)
  touch "$STAMP" 2>/dev/null
}

fmt_dur() {
  local s=$1
  if [ "$s" -ge 3600 ]; then print -r -- "$((s / 3600))h$(((s % 3600) / 60))m"
  elif [ "$s" -ge 60 ]; then print -r -- "$((s / 60))m$((s % 60))s"
  else print -r -- "${s}s"; fi
}

# Interruptible sleep: a trapped signal (USR1 = reopen request) wakes us early.
nap() {
  sleep "$1" &
  local n=$!
  wait $n 2>/dev/null
  kill $n 2>/dev/null
}

# ---- update detection ---------------------------------------------------------
# Claude's updater downloads an update, then quits an idle instance so Squirrel's
# ShipIt can swap the bundle. ShipIt only installs once EVERY process of the
# bundle has quit, and only relaunches the default instance afterwards; so an
# instance that quit for an update must wait for the install, then be reopened.

# Version of the downloaded-but-not-installed update (fails if none). ShipIt
# deletes the unpacked bundle once it has installed it.
staged_update_version() {
  local state="$SHIPIT_DIR/ShipItState.plist" url="" p="" v=""
  [ -f "$state" ] || return 1
  url=$(sed -n 's/.*"updateBundleURL":"\([^"]*\)".*/\1/p' "$state" 2>/dev/null | head -n1)
  [ -n "$url" ] || url=$(/usr/libexec/PlistBuddy -c "Print :updateBundleURL" "$state" 2>/dev/null)
  p=${url//\\\//\/}; p=${p#file://}; p=${p//\%20/ }; p=${p%/}
  [ -n "$p" ] && [ -d "$p" ] || return 1
  v=$(/usr/libexec/PlistBuddy -c "Print :CFBundleVersion" "$p/Contents/Info.plist" 2>/dev/null) || v=unknown
  [ "$v" != "$(bundle_version)" ] || return 1
  print -r -- "$v"
}

# ShipIt processes waiting to install (they run out of the Claude bundle).
shipit_pids() {
  ps -axo pid=,command= | MA="$CLAUDE_APP/" MS="$SHIPIT_DIR" awk '
    BEGIN { a = ENVIRON["MA"]; s = ENVIRON["MS"] }
    index($0, "ShipIt") && (index($0, a) || index($0, s)) { printf "%s%s", (n++ ? "," : ""), $1 }'
}

# Prints a one-line description and succeeds when an update is pending.
update_pending() {
  local v="" p=""
  v=$(staged_update_version) || v=""
  p=$(shipit_pids)
  print -r -- "staged=${v:-none} shipit=${p:-none}"
  [ -n "$v" ] || [ -n "$p" ]
}

# ---- diagnostics ---------------------------------------------------------------
# Data-dir names of OTHER running instances of this Claude bundle ("default" =
# no --user-data-dir). These are what hold up a pending install.
other_instances() {
  ps -axo command= | MB="$(claude_binary)" MD="$DATA_DIR" awk '
    BEGIN { b = ENVIRON["MB"]; me = ENVIRON["MD"] }
    index($0, b) == 1 || index($0, " " b) {
      d = "default"
      if (i = index($0, "--user-data-dir=")) {
        d = substr($0, i + 16); j = index(d, " --"); if (j) d = substr(d, 1, j - 1)
        if (d == me) next
        sub(/.*\//, "", d)
      }
      printf "%s%s", (n++ ? ", " : ""), d
    }
    END { if (!n) printf "none" }'
}

# Quit/update/crash lines from Claude's shared main log in the last $1 seconds.
recent_claude_log() {
  [ -f "$CLAUDE_LOG" ] || return 0
  tail -n 5000 "$CLAUDE_LOG" 2>/dev/null | SINCE="$(date -v-"$1"S '+%Y-%m-%d %H:%M:%S')" awk '
    BEGIN { s = ENVIRON["SINCE"] }
    substr($0, 1, 19) >= s {
      l = tolower($0)
      if (l ~ /\[updater\] (found an update|update downloaded|previous update|.*install)|stealth-relaunch|beforequit|willquit|before-quit|will-quit|crash|fatal|uncaught|process-gone|event-loop-stall/) print substr($0, 1, 240)
    }' | tail -n 20
}

# Snapshot of everything useful for "why did this instance go away?".
log_exit_context() {
  local now upd reports
  now=$(date +%s)
  upd=$(update_pending)
  log "  context: pid=${inst_pid:-?} lived=$(fmt_dur $((now - started_at))) claude=$(bundle_version) launched_on=$last_ver update[$upd]"
  log "  other instances running: $(other_instances)"
  if [ -f "$STAMP" ]; then
    reports=$(find "$HOME/Library/Logs/DiagnosticReports" /Library/Logs/DiagnosticReports -maxdepth 1 -newer "$STAMP" \
      \( -name 'Claude*' -o -name 'JetsamEvent*' \) 2>/dev/null | sed 's/.*\///' | tr '\n' ' ')
    [ -n "$reports" ] && log "  crash/jetsam reports since launch: $reports"
  fi
  recent_claude_log $((POLL + GRACE + 120)) | while IFS= read -r l; do log "  claude log: $l"; done
}

# NOTE: no "focus the running window" helper on purpose. `open -a` can launch
# a stray argless instance, and spawning with the same --user-data-dir proved
# to leave a second full instance running (Claude does not reliably hand off
# via a single-instance lock). Nothing here may risk spawning a duplicate.

launch_instance() {
  last_ver=$(bundle_version)
  REOPEN_REQUESTED=0
  log "launching ($last_ver) with --user-data-dir=$DATA_DIR"
  open -n -a "$CLAUDE_APP" --args --user-data-dir="$DATA_DIR" 2>/dev/null || true
  mark_started
  sleep "$SETTLE"
  instance_running && log "instance up (pid $inst_pid)"
}

launch_and_verify() {
  launch_instance
  instance_running && return 0
  sleep "$GRACE"
  instance_running && return 0
  log "launch did not produce a running instance; retrying once"
  launch_instance
  instance_running && return 0
  sleep "$GRACE"
  instance_running
}

# ---- one watchdog per instance (atomic symlink lock, stale-safe) --------------
if ! ln -s "$$" "$LOCK" 2>/dev/null; then
  oldpid=$(readlink "$LOCK" 2>/dev/null || true)
  if [ -n "$oldpid" ] && kill -0 "$oldpid" 2>/dev/null; then
    # A live watchdog already owns this instance. If the instance is down (the
    # owner is in its grace or update wait), ask the owner to reopen it now;
    # the owner does the launch so there is never a second launcher racing it.
    if instance_running; then
      log "launcher opened; watchdog pid $oldpid owns the running instance (pid $inst_pid); exiting"
    else
      kill -USR1 "$oldpid" 2>/dev/null
      log "launcher opened while instance is down; asked watchdog pid $oldpid to reopen it"
    fi
    exit 0
  fi
  rm -f "$LOCK"
  ln -s "$$" "$LOCK" 2>/dev/null || exit 0
fi
trap 'log "watchdog (pid $$) exiting"; rm -f "$LOCK"' EXIT
trap 'log "watchdog stopped by signal"; exit 0' INT TERM
trap 'REOPEN_REQUESTED=1; log "reopen requested by launcher"' USR1

log "watchdog started (pid $$; poll=${POLL}s grace=${GRACE}s; shipit=$SHIPIT_DIR)"

# ---- adopt a running instance, or launch one ----------------------------------
if instance_running; then
  last_ver=$(bundle_version)
  mark_started
  log "adopting already-running instance ($last_ver, pid $inst_pid)"
else
  if ! launch_and_verify; then
    log "instance failed to start; giving up"
    exit 1
  fi
fi

# ---- update wait ----------------------------------------------------------------
# The instance quit so a pending update could install. Wait (without reopening:
# a running instance blocks the install) until the update lands, the pending
# update disappears, the user asks to reopen, or UPDATE_WAIT_MAX passes; then
# reopen. Returns non-zero only if the reopen fails.
wait_for_update_install() {
  local waited=0 since_beat=0 cur="" upd=""
  while :; do
    nap "$POLL"
    waited=$((waited + POLL)); since_beat=$((since_beat + POLL))
    if instance_running; then
      last_ver=$(bundle_version)
      mark_started
      log "instance reappeared during update wait (pid $inst_pid, $last_ver); continuing to watch"
      return 0
    fi
    cur=$(bundle_version)
    if [ "$cur" != "$last_ver" ]; then
      log "update installed ($last_ver -> $cur) after $(fmt_dur $waited); reopening instance"
      relaunched_for="$cur"
      launch_and_verify; return
    fi
    if [ "$REOPEN_REQUESTED" -eq 1 ]; then
      log "reopening on $cur at launcher request (the update will install after this instance quits again)"
      launch_and_verify; return
    fi
    if ! upd=$(update_pending); then
      log "pending update went away without installing (still $cur) after $(fmt_dur $waited); reopening instance"
      launch_and_verify; return
    fi
    if [ "$waited" -ge "$UPDATE_WAIT_MAX" ]; then
      log "update still not installed after $(fmt_dur $waited) ($upd); reopening on $cur. Still running: $(other_instances)"
      launch_and_verify; return
    fi
    if [ "$since_beat" -ge "$HEARTBEAT" ]; then
      since_beat=0
      log "still waiting for update install ($(fmt_dur $waited); $upd). Install is blocked until these quit: $(other_instances)"
    fi
  done
}

# ---- babysit -------------------------------------------------------------------
# On disappearance: grace-wait for a self-relaunch; else
#   - bundle version changed (update already installed): reopen, once per version
#   - update downloaded but not installed: wait for the install, then reopen
#   - launcher opened meanwhile: reopen
#   - otherwise an intentional quit (or crash): stand down.
relaunched_for=""
staged_seen=""
while :; do
  nap "$POLL"
  if instance_running; then
    staged=$(staged_update_version) || staged=""
    if [ -n "$staged" ] && [ "$staged" != "$staged_seen" ]; then
      log "update $staged downloaded and staged; Claude will quit this instance to install it once idle"
    fi
    staged_seen="$staged"
    continue
  fi
  log "instance disappeared (pid ${inst_pid:-?}); waiting ${GRACE}s for a self-relaunch"
  log_exit_context
  nap "$GRACE"
  if instance_running; then
    last_ver=$(bundle_version)
    mark_started
    log "instance relaunched itself (pid $inst_pid); continuing to watch ($last_ver)"
    continue
  fi
  cur=$(bundle_version)
  if [ "$cur" != "$last_ver" ] && [ "$cur" != "$relaunched_for" ]; then
    log "update detected ($last_ver -> $cur); reopening instance"
    relaunched_for="$cur"
    if ! launch_and_verify; then
      log "reopen after update failed; exiting"
      exit 1
    fi
  elif upd=$(update_pending); then
    log "update pending ($upd) but not installed: Claude quit this instance to install it. ShipIt waits until every instance has quit; still running: $(other_instances)"
    if ! wait_for_update_install; then
      log "reopen after update wait failed; exiting"
      exit 1
    fi
  elif [ "$REOPEN_REQUESTED" -eq 1 ]; then
    log "instance quit but the launcher was opened again; reopening"
    if ! launch_and_verify; then
      log "reopen failed; exiting"
      exit 1
    fi
  else
    log "instance exited with no version change and no pending update; assuming intentional quit"
    exit 0
  fi
done
BODY
chmod +x "$APP_PATH/Contents/MacOS/launcher"

# Borrow Claude's own icon if a loose .icns exists (newer builds may keep it in
# an asset catalog instead; then the launcher just gets a generic icon).
CLAUDE_ICNS=""
for f in "$CLAUDE_APP"/Contents/Resources/*.icns(N); do CLAUDE_ICNS="$f"; break; done
if [ -n "$CLAUDE_ICNS" ]; then
  cp "$CLAUDE_ICNS" "$APP_PATH/Contents/Resources/AppIcon.icns"
fi

# Ad-hoc sign the LAUNCHER (a brand-new app we made) — never Claude's bundle.
codesign --force --sign - "$APP_PATH" >/dev/null 2>&1 || true

# Refresh Launch Services so the name/icon register.
LSR="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
"$LSR" -f "$APP_PATH" >/dev/null 2>&1 || true

echo "Done."
echo ""
echo "Launch it:   open -n \"$APP_PATH\""
echo "Or find \"$DISPLAY_NAME\" in Spotlight / Launchpad."
echo ""
echo "The launcher stays resident (no Dock icon) and reopens this instance if a"
echo "Claude auto-update quits it. Watchdog log:"
echo "   ~/Library/Logs/claude-instances/$DISPLAY_NAME.log"
echo ""
echo "First login: quit your main Claude (Cmd+Q), open this instance, request"
echo "its magic link, and open the link while only this instance is running —"
echo "so the claude:// login routes to it. After that, both run side by side."

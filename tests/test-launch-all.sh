#!/bin/zsh
#
# test-launch-all.sh: end-to-end tests for `new-claude --launch-all`. It
# launches every launcher that is not already running, in order.txt order
# (unlisted launchers after, in C order), with a gap between launches. Runs
# against a stub app bundle so the real Claude.app is never touched; the order
# file lives in a temp config dir via CLAUDE_INSTANCES_CONFIG_DIR.
#
# Usage:  tests/test-launch-all.sh          (takes ~1-2 minutes)

REPO_DIR="${0:A:h:h}"
NEW_CLAUDE="$REPO_DIR/bin/new-claude.sh"

TMP=$(mktemp -d /tmp/claude-launch-all-test.XXXXXX)
APPS="$TMP/Apps"
STUB_APP="$TMP/FakeClaude.app"
STUB_BIN="$STUB_APP/Contents/MacOS/stubclaude"
NAMES=("Claude LA One" "Claude LA Two" "Claude LA Three")
SUPPORT="$HOME/Library/Application Support"
DATA_DIRS=("$SUPPORT/claude-la-test-one" "$SUPPORT/claude-la-test-two" "$SUPPORT/claude-la-test-three")
LOG_DIR="$HOME/Library/Logs/claude-instances"

source "$REPO_DIR/tests/lib/stub-claude.sh"

export CLAUDE_LAUNCHER_POLL_SECS=1
export CLAUDE_LAUNCHER_GRACE_SECS=2
export CLAUDE_LAUNCHER_SETTLE_SECS=1
# Exit diagnostics must never read the real main.log or ShipIt cache.
export CLAUDE_LAUNCHER_CLAUDE_LOG="$TMP/main.log"
export CLAUDE_LAUNCHER_SHIPIT_DIR="$TMP/ShipIt"
mkdir -p "$CLAUDE_LAUNCHER_SHIPIT_DIR"
export CLAUDE_INSTANCES_CONFIG_DIR="$TMP/cfg"
mkdir -p "$CLAUDE_INSTANCES_CONFIG_DIR"
ORDER="$CLAUDE_INSTANCES_CONFIG_DIR/order.txt"

launcher_of() { print -r -- "$APPS/$1.app/Contents/MacOS/launcher"; }
stub_count() { stub_pids | wc -l | tr -d ' '; }
# The names on "launching" lines, comma-joined in output order.
launched() { print -r -- "$1" | sed -n 's/^   • \(.*\): launching$/\1/p' | paste -sd, -; }

# Start a stand-in watchdog for launcher script $1: zsh running a sleeper at that
# exact path, so ps shows `/bin/zsh <script>`. Sets FAKE_WD to its pid. The real
# script is moved aside while the sleeper starts, then put back. (The sleep is
# not the script's last command: zsh would exec that one in place of itself.)
start_fake_watchdog() {
  local script="$1" i
  mv "$script" "$script.real"
  print -r -- '#!/bin/zsh
sleep 60
:' > "$script"
  chmod +x "$script"
  "$script" &
  FAKE_WD=$!
  # Its sleep child shows the sleeper script has been read; only then restore.
  for i in {1..50}; do
    pgrep -P "$FAKE_WD" sleep >/dev/null 2>&1 && break
    sleep 0.1
  done
  mv -f "$script.real" "$script"
}

# Stop all three launchers' watchdogs and every stub, until nothing is left (a
# launcher opened through `open` may start a little late).
reset_all() {
  local i n left
  for i in {1..10}; do
    left=""
    for n in "${NAMES[@]}"; do kill_watchdogs_for "$(launcher_of "$n")"; done
    kill_stub
    sleep 1
    for n in "${NAMES[@]}"; do left+="$(watchdog_pids_for "$(launcher_of "$n")")"; done
    [ -z "$(stub_pids)$left" ] && break
  done
}

cleanup() {
  local n
  reset_all
  rm -rf "$TMP"
  for n in "${NAMES[@]}"; do rm -f "$LOG_DIR/$n.log" "$LOG_DIR/$n.lock" "$LOG_DIR/$n.stamp"; done
}
trap cleanup EXIT INT TERM

# ---- build the stub "Claude" and three launchers -------------------------------
build_stub_app
echo "Building launchers against stub app..."
"$NEW_CLAUDE" "Claude LA One" --apps "$APPS" --claude-app "$STUB_APP" --dir claude-la-test-one >/dev/null
"$NEW_CLAUDE" "Claude LA Two" --apps "$APPS" --claude-app "$STUB_APP" --dir claude-la-test-two >/dev/null
"$NEW_CLAUDE" "Claude LA Three" --apps "$APPS" --claude-app "$STUB_APP" --dir claude-la-test-three >/dev/null
for n in "${NAMES[@]}"; do [ -x "$(launcher_of "$n")" ] || { echo "!! launchers were not generated"; exit 1; }; done
printf 'Claude LA Three\nClaude LA Ghost\nClaude LA Three\nClaude LA One\n' > "$ORDER"

echo ""
echo "Test 1: launches in order.txt order, then the rest, with a 3 s gap"
START=$(date +%s)
OUT=$("$NEW_CLAUDE" --launch-all --apps "$APPS")
ELAPSED=$(( $(date +%s) - START ))
if [ "$(launched "$OUT")" = "Claude LA Three,Claude LA One,Claude LA Two" ]; then
  ok "launch order is Three, One, Two"
else
  bad "launch order is Three, One, Two (got: $(launched "$OUT"))"
fi
if [ -n "$OUT" ] && ! print -r -- "$OUT" | grep -qF "Ghost"; then ok "output does not mention Ghost"; else bad "output does not mention Ghost"; fi
if [ "$ELAPSED" -ge 6 ]; then ok "two gaps between three launches"; else bad "two gaps between three launches (elapsed=${ELAPSED}s)"; fi
sleep 12
for i in 1 2 3; do
  if stub_running_for "${DATA_DIRS[$i]}"; then ok "${NAMES[$i]} is running"; else bad "${NAMES[$i]} is running"; fi
done

echo ""
echo "Test 2: a second --launch-all skips running instances and launches nothing"
OUT=$("$NEW_CLAUDE" --launch-all --apps "$APPS")
if [ "$(print -r -- "$OUT" | grep -c ': already running$')" = "3" ] && ! print -r -- "$OUT" | grep -qF ": launching"; then
  ok "three already running, none launched"
else
  bad "three already running, none launched"
  print -r -- "$OUT" | sed 's/^/      /'
fi
if [ "$(stub_count)" = "3" ]; then ok "still three stubs"; else bad "still three stubs (count=$(stub_count))"; fi

echo ""
echo "Test 3: no order file launches everything in C order"
rm -f "$ORDER"
reset_all
OUT=$("$NEW_CLAUDE" --launch-all --apps "$APPS")
if [ "$(launched "$OUT")" = "Claude LA One,Claude LA Three,Claude LA Two" ]; then
  ok "launch order is One, Three, Two"
else
  bad "launch order is One, Three, Two (got: $(launched "$OUT"))"
fi

echo ""
echo "Test 4: a last order.txt line without a trailing newline is honored"
printf 'Claude LA Two\nClaude LA One' > "$ORDER"
reset_all
OUT=$("$NEW_CLAUDE" --launch-all --apps "$APPS")
if [ "$(launched "$OUT")" = "Claude LA Two,Claude LA One,Claude LA Three" ]; then
  ok "launch order is Two, One, Three"
else
  bad "launch order is Two, One, Three (got: $(launched "$OUT"))"
fi

echo ""
echo "Test 5: an instance whose watchdog waits for an update install is skipped"
reset_all
# A stand-in watchdog: zsh running Two's launcher path (so ps shows the
# `/bin/zsh <script>` line a real watchdog has), named by Two's lock, with an
# update-wait line last in Two's log. The script is swapped for a sleeper only
# while the stand-in starts, then restored.
start_fake_watchdog "$(launcher_of "Claude LA Two")"
ln -sf "$FAKE_WD" "$LOG_DIR/Claude LA Two.lock"
print -r -- "$(date '+%Y-%m-%d %H:%M:%S') update pending (staged=101 shipit=none) but not installed: Claude quit this instance to install it. ShipIt waits until every instance has quit; still running: claude-la-test-one" > "$LOG_DIR/Claude LA Two.log"
print -r -- "$(date '+%Y-%m-%d %H:%M:%S') still waiting for update install (30m0s; staged=101 shipit=none). Install is blocked until these quit: claude-la-test-one" >> "$LOG_DIR/Claude LA Two.log"
OUT=$("$NEW_CLAUDE" --launch-all --apps "$APPS")
if print -r -- "$OUT" | grep -qxF "   • Claude LA Two: waiting for update; skipped"; then
  ok "Two is skipped as waiting for update"
else
  bad "Two is skipped as waiting for update"
  print -r -- "$OUT" | sed 's/^/      /'
fi
if [ "$(launched "$OUT")" = "Claude LA One,Claude LA Three" ]; then
  ok "the others still launch"
else
  bad "the others still launch (got: $(launched "$OUT"))"
fi
kill "$FAKE_WD" 2>/dev/null
wait "$FAKE_WD" 2>/dev/null
rm -f "$LOG_DIR/Claude LA Two.lock"

echo ""
echo "Test 6: a pager viewing the launcher script is not taken for its watchdog"
reset_all
tail -f "$(launcher_of "Claude LA Two")" >/dev/null &
PAGER_PID=$!
ln -sf "$PAGER_PID" "$LOG_DIR/Claude LA Two.lock"
OUT=$("$NEW_CLAUDE" --launch-all --apps "$APPS")
if print -r -- "$OUT" | grep -qxF "   • Claude LA Two: launching"; then
  ok "Two is launched, not skipped as waiting for update"
else
  bad "Two is launched, not skipped as waiting for update"
  print -r -- "$OUT" | sed 's/^/      /'
fi
kill "$PAGER_PID" 2>/dev/null
wait "$PAGER_PID" 2>/dev/null
rm -f "$LOG_DIR/Claude LA Two.lock" "$LOG_DIR/Claude LA Two.log"

echo ""
echo "Test 7: a stale lock naming an unrelated live pid is taken over, never signalled"
reset_all
# A watchdog killed without its EXIT trap leaves its lock; after a reboot the pid
# can belong to anything. SIGUSR1 would end this sleep, so its survival shows it
# was not signalled.
sleep 120 &
UNRELATED=$!
ln -sf "$UNRELATED" "$LOG_DIR/Claude LA One.lock"
"$(launcher_of "Claude LA One")" &
STALE_WD=$!
for i in {1..20}; do stub_running_for "${DATA_DIRS[1]}" && break; sleep 0.5; done
if kill -0 "$UNRELATED" 2>/dev/null; then ok "the unrelated process was not signalled"; else bad "the unrelated process was not signalled"; fi
if stub_running_for "${DATA_DIRS[1]}"; then ok "the launcher launched its instance"; else bad "the launcher launched its instance"; fi
if [ "$(readlink "$LOG_DIR/Claude LA One.lock" 2>/dev/null)" = "$STALE_WD" ]; then
  ok "the lock now names the new watchdog"
else
  bad "the lock now names the new watchdog (lock: $(readlink "$LOG_DIR/Claude LA One.lock" 2>/dev/null))"
fi
if grep -qF "stale lock names pid $UNRELATED" "$LOG_DIR/Claude LA One.log" 2>/dev/null; then
  ok "the takeover is logged"
else
  bad "the takeover is logged"
fi
reset_all
kill "$UNRELATED" 2>/dev/null
wait "$UNRELATED" 2>/dev/null
rm -f "$LOG_DIR/Claude LA One.lock"

echo ""
echo "Test 8: one failed open is reported and the rest still launch; exit is non-zero"
reset_all
rm -f "$ORDER"
# An `open` earlier on PATH that fails for One's launcher and passes the rest on.
mkdir -p "$TMP/bin"
cat > "$TMP/bin/open" <<'OPEN'
#!/bin/zsh
for a in "$@"; do
  case "$a" in *"/Claude LA One.app") echo "open: simulated failure" >&2; exit 1 ;; esac
done
exec /usr/bin/open "$@"
OPEN
chmod +x "$TMP/bin/open"
OUT=$(PATH="$TMP/bin:$PATH" "$NEW_CLAUDE" --launch-all --apps "$APPS" 2>/dev/null)
RC=$?
if print -r -- "$OUT" | grep -qxF "   • Claude LA One: open failed"; then ok "the failed open is reported"; else bad "the failed open is reported"; print -r -- "$OUT" | sed 's/^/      /'; fi
if [ "$(launched "$OUT")" = "Claude LA One,Claude LA Three,Claude LA Two" ]; then
  ok "the instances after it still launch"
else
  bad "the instances after it still launch (got: $(launched "$OUT"))"
fi
if [ "$RC" -ne 0 ]; then ok "exit status is non-zero"; else bad "exit status is non-zero"; fi

echo ""
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]

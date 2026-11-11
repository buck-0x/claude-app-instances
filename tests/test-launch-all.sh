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
echo "Results: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]

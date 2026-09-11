#!/bin/zsh
#
# remove-claude.sh — remove a Claude instance launcher created by new-claude.
#
# By default this removes ONLY the launcher app. The instance's data folder
# (login, history, chats) is left in place unless you pass --purge.
#
# Usage:
#   remove-claude "Claude Fulcra"            # remove launcher, keep data
#   remove-claude "Claude Fulcra" --purge    # remove launcher AND its data
#   remove-claude "Claude Fulcra" --apps ~/Applications

set -e

DISPLAY_NAME=""
APPS_DIR="/Applications"
PURGE=0
SUPPORT_DIR="$HOME/Library/Application Support"

while [ $# -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    --apps)  APPS_DIR="${2/#\~/$HOME}"; shift 2 ;;
    -*)      echo "Unknown option: $1" >&2; exit 1 ;;
    *)       if [ -z "$DISPLAY_NAME" ]; then DISPLAY_NAME="$1"; fi; shift ;;
  esac
done

if [ -z "$DISPLAY_NAME" ]; then
  echo "Usage: remove-claude \"Claude Name\" [--purge] [--apps /install/path]" >&2
  exit 1
fi

APP_PATH="$APPS_DIR/$DISPLAY_NAME.app"

if [ ! -d "$APP_PATH" ]; then
  echo "!! No launcher found at $APP_PATH" >&2
  exit 1
fi

# Confirm this is actually one of our launchers, not some other app.
id=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$APP_PATH/Contents/Info.plist" 2>/dev/null || true)
case "$id" in
  local.launcher.*) : ;;
  *)
    echo "!! $APP_PATH is not a new-claude launcher (id: $id). Refusing to remove." >&2
    exit 1 ;;
esac

# Find the data dir the launcher points at (so --purge targets the right
# folder). Watchdog launchers carry a DATA_DIR= line; old one-shot launchers
# only have it inline in the open command.
LAUNCHER_SCRIPT="$APP_PATH/Contents/MacOS/launcher"
DATA_DIR=$(sed -n 's/^DATA_DIR="\(.*\)"$/\1/p' "$LAUNCHER_SCRIPT" 2>/dev/null | head -n1)
if [ -z "$DATA_DIR" ]; then
  DATA_DIR=$(sed -n 's/.*--user-data-dir="\([^"]*\)".*/\1/p' "$LAUNCHER_SCRIPT" 2>/dev/null | head -n1)
fi

# Stop any resident watchdog for this launcher first, so it can't resurrect
# the instance being removed. (Matches only the launcher script's own process;
# the Claude instance's command line never contains the launcher path.)
PIDS=$(ps -axo pid=,command= | MS="$LAUNCHER_SCRIPT" awk 'BEGIN { s = ENVIRON["MS"] } index($0, s) { print $1 }') || true
if [ -n "$PIDS" ]; then
  echo "Stopping resident watchdog"
  print -r -- "$PIDS" | xargs kill 2>/dev/null || true
fi

echo "Removing launcher: $APP_PATH"
rm -rf "$APP_PATH"

# Watchdog bookkeeping files (log + lock) are keyed to the display name.
rm -f "$HOME/Library/Logs/claude-instances/$DISPLAY_NAME.log" \
      "$HOME/Library/Logs/claude-instances/$DISPLAY_NAME.lock"

if [ "$PURGE" -eq 1 ]; then
  if [ -n "$DATA_DIR" ] && [ -d "$DATA_DIR" ]; then
    echo "Purging data folder: $DATA_DIR"
    rm -rf "$DATA_DIR"
  else
    echo "No data folder found to purge."
  fi
else
  if [ -n "$DATA_DIR" ]; then
    echo "Kept data folder: $DATA_DIR"
    echo "(remove it with --purge, or delete it manually)"
  fi
fi

echo "Done."

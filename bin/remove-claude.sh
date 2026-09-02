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

# Find the data dir the launcher points at (so --purge targets the right folder).
DATA_DIR=$(sed -n 's/.*--user-data-dir="\([^"]*\)".*/\1/p' "$APP_PATH/Contents/MacOS/launcher" 2>/dev/null)

echo "Removing launcher: $APP_PATH"
rm -rf "$APP_PATH"

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

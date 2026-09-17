#!/bin/zsh
#
# new-chatgpt.sh — create a launcher app for a second, isolated ChatGPT instance.
#
# Each launcher runs the REAL, unmodified ChatGPT.app with its own
# --user-data-dir (Chromium profile: cookies, web login) AND its own
# CODEX_HOME (app data: chats, sessions, auth; default ~/.codex), so every
# instance is fully separate. ChatGPT's own bundle is never touched or
# re-signed. Both stores live under one data folder per instance
# (verified: instances run side by side without sharing anything).
#
# Usage:
#   new-chatgpt "ChatGPT Fulcra"
#   new-chatgpt "ChatGPT Fulcra" --dir "ChatGPT-Fulcra"   # custom data folder
#   new-chatgpt "ChatGPT Fulcra" --apps ~/Applications    # custom install dir
#   new-chatgpt --list                                    # list launchers created
#
# Re-running with the same name safely rebuilds that launcher.

set -e

# ---- config ------------------------------------------------------------------
CHATGPT_APP="/Applications/ChatGPT.app"
SUPPORT_DIR="$HOME/Library/Application Support"

# ---- parse arguments ---------------------------------------------------------
DISPLAY_NAME=""
DATA_DIR_NAME=""
APPS_DIR="/Applications"
DO_LIST=0

while [ $# -gt 0 ]; do
  case "$1" in
    --list) DO_LIST=1; shift ;;
    --dir)  DATA_DIR_NAME="$2"; shift 2 ;;
    --apps) APPS_DIR="${2/#\~/$HOME}"; shift 2 ;;
    -h|--help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*)     echo "Unknown option: $1" >&2; exit 1 ;;
    *)      if [ -z "$DISPLAY_NAME" ]; then DISPLAY_NAME="$1"; fi; shift ;;
  esac
done

# ---- --list mode -------------------------------------------------------------
if [ "$DO_LIST" -eq 1 ]; then
  echo "ChatGPT instance launchers found:"
  found=0
  for plist in "$APPS_DIR"/*.app/Contents/Info.plist(N); do
    [ -f "$plist" ] || continue
    id=$(/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "$plist" 2>/dev/null || true)
    case "$id" in
      local.launcher-chatgpt.*)
        app="${plist%/Contents/Info.plist}"
        name=$(/usr/libexec/PlistBuddy -c "Print :CFBundleDisplayName" "$plist" 2>/dev/null)
        datadir=$(sed -n 's/.*--user-data-dir="\([^"]*\)".*/\1/p' "$app/Contents/MacOS/launcher" 2>/dev/null)
        echo "   • $name"
        echo "       app:  $app"
        echo "       data: $datadir"
        found=1 ;;
    esac
  done
  [ "$found" -eq 0 ] && echo "   (none yet — create one with: new-chatgpt \"ChatGPT Name\")"
  exit 0
fi

if [ -z "$DISPLAY_NAME" ]; then
  echo "Usage: new-chatgpt \"ChatGPT Name\" [--dir DataFolderName] [--apps /install/path]" >&2
  echo "       new-chatgpt --list" >&2
  exit 1
fi

# ---- derive names ------------------------------------------------------------
# Default data folder: display name with spaces -> hyphens.
#   "ChatGPT Fulcra" -> "ChatGPT-Fulcra"
if [ -z "$DATA_DIR_NAME" ]; then
  DATA_DIR_NAME="${DISPLAY_NAME// /-}"
fi

DATA_DIR="$SUPPORT_DIR/$DATA_DIR_NAME"
APP_PATH="$APPS_DIR/$DISPLAY_NAME.app"

# ---- sanity checks -----------------------------------------------------------
if [ ! -d "$CHATGPT_APP" ]; then
  echo "!! $CHATGPT_APP not found. Install ChatGPT first from https://chatgpt.com/download" >&2
  exit 1
fi

echo "Creating launcher:"
echo "   Name : $DISPLAY_NAME"
echo "   App  : $APP_PATH"
echo "   Data : $DATA_DIR"
echo ""

# ---- build the .app bundle ---------------------------------------------------
# Same structure Automator produces for an "Application", built directly so no
# GUI is needed. A tiny app whose only job is to run one shell command.

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
    <key>CFBundleIdentifier</key>      <string>local.launcher-chatgpt.${DATA_DIR_NAME:l}</string>
    <key>CFBundleVersion</key>         <string>1.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleExecutable</key>      <string>launcher</string>
    <key>CFBundleIconFile</key>        <string>AppIcon</string>
    <key>LSUIElement</key>             <false/>
    <key>NSHighResolutionCapable</key> <true/>
</dict>
</plist>
PLIST

# The executable launches the REAL ChatGPT with its own data dir AND its own
# CODEX_HOME. --user-data-dir isolates only the Chromium profile (cookies,
# web login); the app keeps chats, sessions, and auth in CODEX_HOME (default
# ~/.codex), which would otherwise be shared by every instance, leaking one
# account's chats into another. Environment variables do not survive `open`
# (launchd starts the app), so the launcher execs the binary directly.
# CODEX_HOME lives inside the data dir so remove-chatgpt --purge wipes both.
cat > "$APP_PATH/Contents/MacOS/launcher" <<LAUNCH
#!/bin/zsh
export CODEX_HOME="$DATA_DIR/codex-home"
mkdir -p "\$CODEX_HOME"
exec "$CHATGPT_APP/Contents/MacOS/ChatGPT" --user-data-dir="$DATA_DIR"
LAUNCH
chmod +x "$APP_PATH/Contents/MacOS/launcher"

# Borrow ChatGPT's own icon if a loose .icns exists (current builds ship
# Contents/Resources/app.icns; if a future build moves it into an asset
# catalog the launcher just gets a generic icon).
CHATGPT_ICNS="$(/bin/ls "$CHATGPT_APP"/Contents/Resources/*.icns 2>/dev/null | head -n1 || true)"
if [ -n "$CHATGPT_ICNS" ]; then
  cp "$CHATGPT_ICNS" "$APP_PATH/Contents/Resources/AppIcon.icns"
fi

# Ad-hoc sign the LAUNCHER (a brand-new app we made) — never ChatGPT's bundle.
codesign --force --sign - "$APP_PATH" >/dev/null 2>&1 || true

# Refresh Launch Services so the name/icon register.
LSR="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
"$LSR" -f "$APP_PATH" >/dev/null 2>&1 || true

echo "Done."
echo ""
echo "Launch it:   open \"$APP_PATH\""
echo "Or find \"$DISPLAY_NAME\" in Spotlight / Launchpad."
echo ""
echo "First login: just sign in inside the new window. Login, chats, and"
echo "settings all live in this instance's own data folder (including its"
echo "private CODEX_HOME), so nothing is shared with other instances."

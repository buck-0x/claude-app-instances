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
# Usage:
#   new-claude "Claude Fulcra"
#   new-claude "Claude Fulcra" --dir "Claude-Fulcra"    # custom data folder
#   new-claude "Claude Fulcra" --apps ~/Applications     # custom install dir
#   new-claude --list                                    # list launchers created
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

while [ $# -gt 0 ]; do
  case "$1" in
    --list) DO_LIST=1; shift ;;
    --dir)  DATA_DIR_NAME="$2"; shift 2 ;;
    --apps) APPS_DIR="${2/#\~/$HOME}"; shift 2 ;;
    -h|--help)
      sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    -*)     echo "Unknown option: $1" >&2; exit 1 ;;
    *)      if [ -z "$DISPLAY_NAME" ]; then DISPLAY_NAME="$1"; fi; shift ;;
  esac
done

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
        datadir=$(sed -n 's/.*--user-data-dir="\([^"]*\)".*/\1/p' "$app/Contents/MacOS/launcher" 2>/dev/null)
        echo "   • $name"
        echo "       app:  $app"
        echo "       data: $datadir"
        found=1 ;;
    esac
  done
  [ "$found" -eq 0 ] && echo "   (none yet — create one with: new-claude \"Claude Name\")"
  exit 0
fi

if [ -z "$DISPLAY_NAME" ]; then
  echo "Usage: new-claude \"Claude Name\" [--dir DataFolderName] [--apps /install/path]" >&2
  echo "       new-claude --list" >&2
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
    <key>CFBundleIdentifier</key>      <string>local.launcher.${DATA_DIR_NAME:l}</string>
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

# The executable launches the REAL Claude with its own data dir. Because the
# untouched, Anthropic-signed Claude.app is what runs, its signature stays
# intact and it does not crash.
cat > "$APP_PATH/Contents/MacOS/launcher" <<LAUNCH
#!/bin/zsh
exec open -n -a "$CLAUDE_APP" --args --user-data-dir="$DATA_DIR"
LAUNCH
chmod +x "$APP_PATH/Contents/MacOS/launcher"

# Borrow Claude's own icon if a loose .icns exists (newer builds may keep it in
# an asset catalog instead; then the launcher just gets a generic icon).
CLAUDE_ICNS="$(/bin/ls "$CLAUDE_APP"/Contents/Resources/*.icns 2>/dev/null | head -n1 || true)"
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
echo "Launch it:   open \"$APP_PATH\""
echo "Or find \"$DISPLAY_NAME\" in Spotlight / Launchpad."
echo ""
echo "First login: quit your main Claude (Cmd+Q), open this instance, request"
echo "its magic link, and open the link while only this instance is running —"
echo "so the claude:// login routes to it. After that, both run side by side."

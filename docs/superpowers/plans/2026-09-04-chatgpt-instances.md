# ChatGPT Instance Launchers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `new-chatgpt` / `remove-chatgpt` scripts that create and remove isolated ChatGPT desktop instances, mirroring the existing Claude launcher pair.

**Architecture:** Each launcher is a tiny generated `.app` bundle whose executable runs `open -n -a /Applications/ChatGPT.app --args --user-data-dir="<own folder>"`. The signed ChatGPT bundle is never modified. ChatGPT launchers use the bundle-ID prefix `local.launcher-chatgpt.*`, which does not glob-match the Claude scripts' `local.launcher.*` pattern, so each tool only sees its own launchers.

**Tech Stack:** zsh scripts, PlistBuddy, codesign (ad-hoc), lsregister. No test framework exists in this repo; each task ends with concrete manual verification commands.

## Global Constraints

- Do NOT modify `bin/new-claude.sh` or `bin/remove-claude.sh` in any way.
- ChatGPT launcher bundle IDs: `local.launcher-chatgpt.<data-dir-name lowercased>` (hyphen after `launcher`, exactly).
- Default data dir: `~/Library/Application Support/<Display Name with spaces replaced by hyphens>`.
- Target app path: `/Applications/ChatGPT.app`; if missing, error pointing to https://chatgpt.com/download.
- Scripts are zsh (`#!/bin/zsh`), `set -e`, and match the style of the existing Claude scripts.
- Verification launchers are built into a scratch dir via `--apps`, never into `/Applications`.

---

### Task 1: `bin/new-chatgpt.sh`

**Files:**
- Create: `bin/new-chatgpt.sh`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: launcher `.app` bundles with bundle ID `local.launcher-chatgpt.<name>`, whose `Contents/MacOS/launcher` contains a `--user-data-dir="<path>"` line. Task 2's remove script greps that exact line and matches that exact bundle-ID prefix. CLI: `new-chatgpt "Name" [--dir NAME] [--apps PATH] [--list] [-h]`.

- [ ] **Step 1: Write the script**

Create `bin/new-chatgpt.sh` with exactly this content:

```zsh
#!/bin/zsh
#
# new-chatgpt.sh — create a launcher app for a second, isolated ChatGPT instance.
#
# Each launcher opens the REAL, unmodified ChatGPT.app with its own
# --user-data-dir, so it gets a separate login, history, and settings.
# ChatGPT's own bundle is never touched or re-signed. The app is a
# Chromium-based build, so --user-data-dir works exactly as it does for
# Claude (verified: a second instance runs side by side with its own
# profile while the main one keeps its default profile).
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
  for plist in "$APPS_DIR"/*.app/Contents/Info.plist; do
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

# The executable launches the REAL ChatGPT with its own data dir. Because the
# untouched, OpenAI-signed ChatGPT.app is what runs, its signature stays
# intact and it does not crash.
cat > "$APP_PATH/Contents/MacOS/launcher" <<LAUNCH
#!/bin/zsh
exec open -n -a "$CHATGPT_APP" --args --user-data-dir="$DATA_DIR"
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
echo "First login: just sign in inside the new window — the session is stored"
echo "in this instance's own data folder, so each instance keeps its own login."
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x bin/new-chatgpt.sh`

- [ ] **Step 3: Verify help and empty list**

Run: `./bin/new-chatgpt.sh --help`
Expected: usage text from the header comment, exit 0.

Run: `mkdir -p /tmp/chatgpt-launcher-test && ./bin/new-chatgpt.sh --list --apps /tmp/chatgpt-launcher-test`
Expected: `(none yet — create one with: new-chatgpt "ChatGPT Name")`.

Run: `./bin/new-chatgpt.sh` (no args)
Expected: usage error on stderr, exit code 1.

- [ ] **Step 4: Build a test launcher and inspect the bundle**

Run: `./bin/new-chatgpt.sh "ChatGPT LauncherTest" --apps /tmp/chatgpt-launcher-test`
Expected: "Creating launcher:" block then "Done."

Run: `/usr/libexec/PlistBuddy -c "Print :CFBundleIdentifier" "/tmp/chatgpt-launcher-test/ChatGPT LauncherTest.app/Contents/Info.plist"`
Expected: `local.launcher-chatgpt.chatgpt-launchertest`

Run: `cat "/tmp/chatgpt-launcher-test/ChatGPT LauncherTest.app/Contents/MacOS/launcher"`
Expected: an `exec open -n -a "/Applications/ChatGPT.app" --args --user-data-dir="$HOME/Library/Application Support/ChatGPT-LauncherTest"` line (with `$HOME` expanded).

Run: `ls "/tmp/chatgpt-launcher-test/ChatGPT LauncherTest.app/Contents/Resources/"`
Expected: `AppIcon.icns` present.

- [ ] **Step 5: Verify --list shows it and Claude's list does not**

Run: `./bin/new-chatgpt.sh --list --apps /tmp/chatgpt-launcher-test`
Expected: lists "ChatGPT LauncherTest" with app and data paths.

Run: `./bin/new-claude.sh --list --apps /tmp/chatgpt-launcher-test`
Expected: `(none yet — ...)` — the Claude script must NOT see the ChatGPT launcher.

- [ ] **Step 6: Commit**

```bash
git add bin/new-chatgpt.sh
git commit -m "feat: add new-chatgpt launcher script"
```

---

### Task 2: `bin/remove-chatgpt.sh`

**Files:**
- Create: `bin/remove-chatgpt.sh`

**Interfaces:**
- Consumes: launcher bundles produced by Task 1 — bundle ID prefix `local.launcher-chatgpt.` and the `--user-data-dir="<path>"` line inside `Contents/MacOS/launcher`.
- Produces: CLI `remove-chatgpt "Name" [--purge] [--apps PATH]`.

- [ ] **Step 1: Write the script**

Create `bin/remove-chatgpt.sh` with exactly this content:

```zsh
#!/bin/zsh
#
# remove-chatgpt.sh — remove a ChatGPT instance launcher created by new-chatgpt.
#
# By default this removes ONLY the launcher app. The instance's data folder
# (login, history, settings) is left in place unless you pass --purge.
#
# Usage:
#   remove-chatgpt "ChatGPT Fulcra"            # remove launcher, keep data
#   remove-chatgpt "ChatGPT Fulcra" --purge    # remove launcher AND its data
#   remove-chatgpt "ChatGPT Fulcra" --apps ~/Applications

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
  echo "Usage: remove-chatgpt \"ChatGPT Name\" [--purge] [--apps /install/path]" >&2
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
  local.launcher-chatgpt.*) : ;;
  *)
    echo "!! $APP_PATH is not a new-chatgpt launcher (id: $id). Refusing to remove." >&2
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
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x bin/remove-chatgpt.sh`

- [ ] **Step 3: Verify it refuses non-ChatGPT launchers**

Build a Claude test launcher next to the ChatGPT one (Claude launchers use the `local.launcher.` prefix, which must be rejected):

Run: `./bin/new-claude.sh "Claude LauncherTest" --apps /tmp/chatgpt-launcher-test`

Run: `./bin/remove-chatgpt.sh "Claude LauncherTest" --apps /tmp/chatgpt-launcher-test`
Expected: `!! ... is not a new-chatgpt launcher (id: local.launcher.claude-launchertest). Refusing to remove.` — exit code 1, app still present.

Run: `./bin/remove-claude.sh "ChatGPT LauncherTest" --apps /tmp/chatgpt-launcher-test`
Expected: `Refusing to remove.` for the ChatGPT launcher (id starts with `local.launcher-chatgpt.`), exit code 1.

- [ ] **Step 4: Verify it removes its own launcher (keeping data)**

Run: `./bin/remove-chatgpt.sh "ChatGPT LauncherTest" --apps /tmp/chatgpt-launcher-test`
Expected: "Removing launcher: ..." then "Kept data folder: ..." then "Done." The `.app` is gone; no data folder was deleted (none was ever created, since the launcher was never opened — the "Kept data folder" line just echoes the path).

Run: `./bin/remove-chatgpt.sh "ChatGPT LauncherTest" --apps /tmp/chatgpt-launcher-test`
Expected: `!! No launcher found at ...`, exit code 1.

- [ ] **Step 5: Clean up the Claude test launcher**

Run: `./bin/remove-claude.sh "Claude LauncherTest" --apps /tmp/chatgpt-launcher-test`
Expected: removed successfully.

Run: `rm -rf /tmp/chatgpt-launcher-test`

- [ ] **Step 6: Commit**

```bash
git add bin/remove-chatgpt.sh
git commit -m "feat: add remove-chatgpt script"
```

---

### Task 3: Wire into `install.sh`

**Files:**
- Modify: `install.sh:16-22` (the chmod and symlink block, and the echo block below it)

**Interfaces:**
- Consumes: `bin/new-chatgpt.sh` (Task 1), `bin/remove-chatgpt.sh` (Task 2).
- Produces: `~/.local/bin/new-chatgpt` and `~/.local/bin/remove-chatgpt` symlinks.

- [ ] **Step 1: Extend the chmod and symlink lines**

In `install.sh`, replace:

```zsh
chmod +x "$BIN_SRC/new-claude.sh" "$BIN_SRC/remove-claude.sh"

ln -sf "$BIN_SRC/new-claude.sh"    "$BIN_DST/new-claude"
ln -sf "$BIN_SRC/remove-claude.sh" "$BIN_DST/remove-claude"

echo "Linked:"
echo "   $BIN_DST/new-claude    -> $BIN_SRC/new-claude.sh"
echo "   $BIN_DST/remove-claude -> $BIN_SRC/remove-claude.sh"
```

with:

```zsh
chmod +x "$BIN_SRC/new-claude.sh" "$BIN_SRC/remove-claude.sh" \
         "$BIN_SRC/new-chatgpt.sh" "$BIN_SRC/remove-chatgpt.sh"

ln -sf "$BIN_SRC/new-claude.sh"     "$BIN_DST/new-claude"
ln -sf "$BIN_SRC/remove-claude.sh"  "$BIN_DST/remove-claude"
ln -sf "$BIN_SRC/new-chatgpt.sh"    "$BIN_DST/new-chatgpt"
ln -sf "$BIN_SRC/remove-chatgpt.sh" "$BIN_DST/remove-chatgpt"

echo "Linked:"
echo "   $BIN_DST/new-claude     -> $BIN_SRC/new-claude.sh"
echo "   $BIN_DST/remove-claude  -> $BIN_SRC/remove-claude.sh"
echo "   $BIN_DST/new-chatgpt    -> $BIN_SRC/new-chatgpt.sh"
echo "   $BIN_DST/remove-chatgpt -> $BIN_SRC/remove-chatgpt.sh"
```

- [ ] **Step 2: Run the installer and verify the links**

Run: `./install.sh`
Expected: four "Linked:" lines, no errors.

Run: `ls -la ~/.local/bin/new-chatgpt ~/.local/bin/remove-chatgpt`
Expected: both symlinks point into this repo's `bin/`.

Run: `~/.local/bin/new-chatgpt --help`
Expected: usage text, exit 0.

- [ ] **Step 3: Commit**

```bash
git add install.sh
git commit -m "feat: install new-chatgpt/remove-chatgpt symlinks"
```

---

### Task 4: README section

**Files:**
- Modify: `README.md` (insert a "ChatGPT instances" section between the "First login (important)" section and the "Notes" section; also update the intro's Install/Requirements wording where noted below)

**Interfaces:**
- Consumes: the CLI names and flags from Tasks 1-2 (`new-chatgpt`, `remove-chatgpt`, `--dir`, `--apps`, `--list`, `--purge`).
- Produces: user-facing documentation only.

**Note:** `README.md` has uncommitted local edits from before this work. Commit only the hunks this task adds (use `git add -p README.md` and stage only the ChatGPT-section and Requirements hunks), leaving the user's other edits unstaged.

- [ ] **Step 1: Insert the ChatGPT section**

Insert this between the "First login (important)" section and the "Notes" section (keeping the `---` separators around it):

````markdown
## ChatGPT instances

The same trick works for OpenAI's **ChatGPT desktop app** — it is a
Chromium-based build, so it honors `--user-data-dir` just like Claude:

```sh
new-chatgpt "ChatGPT MyCompany"
```

All the same flags apply (`--dir`, `--apps`, `--list`), and removal mirrors
`remove-claude`:

```sh
remove-chatgpt "ChatGPT MyCompany"           # keep data
remove-chatgpt "ChatGPT MyCompany" --purge   # wipe data too
```

Differences from Claude worth knowing:

- **Login is simpler.** ChatGPT signs in inside its own window and stores the
  session in that instance's data folder — no magic-link routing dance. Just
  open the new instance and sign in.
- **`codex://` links.** The app registers a `codex://` URL scheme (and can
  act as an http/https handler). macOS routes such links to whichever
  instance registered the scheme most recently — same caveat as Claude's
  `claude://` links, but it only affects "open in app" links, not login.
- **Full profile isolation.** Each instance is a separate Chromium profile:
  its own login, history, and settings.

Requires ChatGPT.app installed at `/Applications/ChatGPT.app`
(from https://chatgpt.com/download).
````

- [ ] **Step 2: Update the Requirements section**

In the "Requirements" section, change:

```markdown
- Claude desktop app installed at `/Applications/Claude.app`
  (from https://claude.ai/download)
```

to:

```markdown
- Claude desktop app installed at `/Applications/Claude.app`
  (from https://claude.ai/download)
- For ChatGPT instances: ChatGPT desktop app installed at
  `/Applications/ChatGPT.app` (from https://chatgpt.com/download)
```

- [ ] **Step 3: Proofread rendered output**

Run: `git diff README.md`
Check: fenced code blocks are balanced, the new section sits between "First login (important)" and "Notes", links are correct.

- [ ] **Step 4: Commit only this task's hunks**

```bash
git add -p README.md
```

Stage only the ChatGPT-section and Requirements hunks; leave pre-existing unrelated edits unstaged. Then:

```bash
git commit -m "docs: document ChatGPT instance support"
```

---

### Task 5: End-to-end live verification

**Files:**
- None created or modified; this task validates the spec's Testing section against the real app.

**Interfaces:**
- Consumes: the installed `new-chatgpt` / `remove-chatgpt` commands (Tasks 1-3).
- Produces: confirmation that an isolated instance actually runs; no repo changes.

**Note:** This task opens a ChatGPT window on the user's machine. The main ChatGPT instance may be running; that is fine and is part of what's being verified.

- [ ] **Step 1: Build a real test launcher (scratch apps dir)**

Run: `mkdir -p /tmp/chatgpt-e2e && new-chatgpt "ChatGPT E2ETest" --apps /tmp/chatgpt-e2e --dir "ChatGPT-E2ETest"`
Expected: "Done."

- [ ] **Step 2: Open it and confirm isolation**

Run: `open "/tmp/chatgpt-e2e/ChatGPT E2ETest.app"`

Wait ~8 seconds, then:

Run: `pgrep -fl "ChatGPT-E2ETest" | head -3`
Expected: a `ChatGPT --user-data-dir=.../Application Support/ChatGPT-E2ETest` process.

Run: `ls "$HOME/Library/Application Support/ChatGPT-E2ETest" | head -5`
Expected: a populated Chromium profile (`Default`, `Local State`, etc.).

- [ ] **Step 3: Quit only the test instance**

Run: `pkill -f "user-data-dir=$HOME/Library/Application Support/ChatGPT-E2ETest"`

Then confirm the main instance is untouched:

Run: `pgrep -fl "MacOS/ChatGPT" | head -3`
Expected: if the main ChatGPT was running before, it still is; the E2ETest process is gone.

- [ ] **Step 4: Remove launcher and purge its data**

Run: `remove-chatgpt "ChatGPT E2ETest" --apps /tmp/chatgpt-e2e --purge`
Expected: "Removing launcher", "Purging data folder: .../ChatGPT-E2ETest", "Done."

Run: `ls "$HOME/Library/Application Support/ChatGPT-E2ETest" 2>&1`
Expected: No such file or directory.

Run: `rm -rf /tmp/chatgpt-e2e`

- [ ] **Step 5: Report results**

No commit — summarize the verification results (isolation confirmed, list/remove cross-checks passed) to the user.

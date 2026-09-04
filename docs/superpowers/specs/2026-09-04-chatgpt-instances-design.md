# ChatGPT Instance Launchers — Design

**Date:** 2026-09-04
**Status:** Approved

## Goal

Extend claude-instances to also create isolated instances of the ChatGPT
desktop app, using the same never-touch-the-signed-bundle launcher approach
that `new-claude` uses for Claude.

## Feasibility (verified 2026-09-04)

- ChatGPT.app (version 26.825.41651, bundle `com.openai.codex`) is a
  Chromium-based "Codex" build. Its engine binary supports `user-data-dir`.
- Verified live: launching `open -n -a /Applications/ChatGPT.app --args
  --user-data-dir=<scratch dir>` created a second, fully isolated instance
  (own Chromium profile) running side by side with the main instance.
- The main app internally uses `~/Library/Application Support/Codex` as its
  profile; named instances get their own folders under Application Support.
- Login is browser-style: sign-in happens inside the window and the session
  lands in that instance's own profile. No magic-link routing dance as with
  Claude. No Keychain-stored auth was found, so account isolation per data
  dir holds.
- Caveat to document: ChatGPT registers a `codex://` URL scheme (plus
  http/https); "open in app" links route to whichever instance registered
  the scheme most recently.

## Structure

Parallel scripts mirroring the existing Claude pair. The Claude scripts are
not modified at all.

### bin/new-chatgpt.sh

- Builds `<apps dir>/<Display Name>.app`, a tiny launcher whose executable
  runs:
  `exec open -n -a "/Applications/ChatGPT.app" --args --user-data-dir="<data dir>"`
- Default data dir: `~/Library/Application Support/<Display-Name-hyphenated>`
  (same derivation as `new-claude`: spaces to hyphens).
- Same flags as `new-claude`: `--dir <name>`, `--apps <path>`, `--list`,
  `-h`/`--help`.
- Bundle ID: `local.launcher-chatgpt.<data dir name, lowercased>`. The
  distinct prefix does not glob-match `local.launcher.*`, so the existing
  Claude scripts (and already-installed Claude launchers) are unaffected,
  and each tool's `--list` and remove safety check only see their own
  launchers.
- Borrows ChatGPT's loose `Contents/Resources/app.icns` for the launcher
  icon.
- Ad-hoc signs the launcher only; refreshes Launch Services via `lsregister`.
- Sanity check: `/Applications/ChatGPT.app` must exist, with a pointer to
  https://chatgpt.com/download if not.
- Re-running with the same name safely rebuilds that launcher.

### bin/remove-chatgpt.sh

- Mirrors `remove-claude.sh`: removes the launcher, keeps the data folder by
  default, `--purge` wipes it, `--apps <path>` for a custom install dir.
- Safety check accepts only `local.launcher-chatgpt.*` bundle IDs, so it can
  never remove a Claude launcher (and `remove-claude` can never remove a
  ChatGPT one).

### install.sh

- Additionally chmods and symlinks `new-chatgpt` and `remove-chatgpt` into
  `~/.local/bin`.

### README.md

- New "ChatGPT instances" section: usage examples, requirement that
  ChatGPT.app is installed, and the differences from Claude — sign in
  directly in the new window (no magic-link dance), the `codex://`
  last-registered-wins caveat, and that each instance's Chromium profile
  (history, login, extensions) is fully separate.

## Error handling

Same patterns as the Claude scripts: `set -e`, explicit sanity checks with
actionable messages, refusal to remove apps that are not our launchers.

## Testing

Manual verification after implementation:

1. Build a test launcher with `new-chatgpt`, open it, and confirm an
   isolated instance runs alongside the main ChatGPT (separate data folder
   gets created and populated).
2. `new-chatgpt --list` shows only ChatGPT launchers; `new-claude --list`
   shows only Claude launchers.
3. `remove-chatgpt` removes the test launcher and refuses a Claude launcher;
   `remove-claude` refuses a ChatGPT launcher.
4. Clean up the test launcher and its data folder.

## Out of scope

- No changes to `new-claude.sh` / `remove-claude.sh`.
- No generic any-app launcher tool.

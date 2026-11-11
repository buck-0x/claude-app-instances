# claude-instances

Run multiple isolated instances of the **Claude desktop app** on macOS — each
with its own account login, chat history, MCP servers, and Cowork environment —
without ever modifying, copying, or re-signing Claude itself.

Your existing Claude stays the default and is never touched.

```sh
new-claude "Claude MyCompany"
```

That builds `/Applications/Claude MyCompany.app`, a small launcher that opens the
real Claude pointed at its own data directory. Run it again with a different
name for a third, fourth, etc.

---

## Why this approach

Each Claude instance stores everything in one folder — by default
`~/Library/Application Support/Claude`. Point a launch at a *different* folder
with Electron's `--user-data-dir` flag and you get a fully independent instance.

The obvious alternative — duplicating `Claude.app` into a renamed
`Claude Work.app` and editing its bundle ID — **does not work on recent Claude
builds** (tested on Claude 1.15962.1 / Electron 42, macOS 15.5, Apple Silicon):

- Editing the bundle breaks its code signature.
- Re-signing it ad-hoc strips Anthropic's Developer ID, and the app's own
  integrity check aborts at startup with `EXC_BREAKPOINT` in `ElectronMain`.
- The renamed-binary wrapper trick hits the identical crash.

It's a catch-22: you can't edit the bundle without re-signing, and re-signing is
what breaks it.

**This tool avoids all of that** by never modifying Claude. It generates a tiny
separate launcher app (the same structure Automator produces) whose only job is:

```sh
open -n -a "/Applications/Claude.app" --args --user-data-dir="<your folder>"
```

Because the untouched, Anthropic-signed `Claude.app` is what actually runs, its
signature stays valid, it never crashes, and Claude's normal auto-updater keeps
working.

---

## Install

```sh
cd ~/devOS/claude-instances
./install.sh
```

This symlinks `new-claude` and `remove-claude` into `~/.local/bin`. If that
directory isn't on your `PATH`, the installer prints the one line to add to
`~/.zshrc`.

You can also just run the scripts directly without installing:

```sh
./bin/new-claude.sh "Claude MyCompany"
```

---

## Usage

Create an instance launcher:

```sh
new-claude "Claude MyCompany"
```

Custom data folder name (default is the display name with spaces → hyphens):

```sh
new-claude "Claude MyCompany" --dir "MyCompany-Data"
```

Install to a different apps folder:

```sh
new-claude "Claude MyCompany" --apps ~/Applications
```

List the launchers you've created:

```sh
new-claude --list
```

Reopen every instance that isn't currently running (e.g. after an update):

```sh
new-claude --reopen-all
```

Launch every instance that isn't running, in the order you set in the
instances TUI (saved in `~/.config/claude-instances/order.txt`; launchers not
listed there follow in name order):

```sh
new-claude --launch-all
```

Remove a launcher (keeps its data by default):

```sh
remove-claude "Claude MyCompany"
```

Remove a launcher **and** wipe its data folder:

```sh
remove-claude "Claude MyCompany" --purge
```

Re-running `new-claude` with the same name safely rebuilds that launcher.

---

## First login (important)

Claude signs in through `claude://` magic links, and macOS hands the link to
whichever instance registered that URL scheme most recently. To make sure a new
instance gets its own login:

1. Fully quit your main Claude with **Cmd+Q** (not just close the window).
2. Open the new instance and request its magic link.
3. Open the link from your email while **only** the new instance is running.

After that first login, all instances stay signed in and run side by side.

> **Security:** a magic link is a one-time login secret. Anyone with the full
> URL can sign into that account. Never paste it into a chat, ticket, or
> message. If one is exposed, let it expire and request a fresh one.

---

## Surviving auto-updates

All instances run the same `/Applications/Claude.app`, and Claude's auto-updater
quietly quits each running instance so it can swap that bundle. The relaunch
doesn't reliably carry `--user-data-dir` for extra instances, so without help
they collapse down to one after every update.

Worse, the order is usually reversed from what you'd expect: each instance
downloads the update, then quits itself once it's idle (no tasks running), but
Squirrel's ShipIt only installs after **every** Claude process has quit. So
idle instances close one by one, hours apart, while busy ones keep the install
blocked, and nothing reopens the ones that closed.

The launcher handles this: it stays resident as a tiny watchdog (no Dock icon)
after starting its instance. When the instance disappears:

- **Bundle version changed** (update already installed): reopen it, at most
  once per new version, so it can never thrash.
- **Update downloaded but not yet installed** (a staged bundle in
  `~/Library/Caches/com.anthropic.claudefordesktop.ShipIt/`, or a ShipIt
  process waiting): don't reopen yet, since a running instance would block the
  install again. Wait for the install, then reopen. If the pending update goes
  away without installing, or 4 hours pass, it reopens anyway.
- **Neither**: you quit it yourself; the watchdog stands down.

Details worth knowing:

- Watchdog logs live in `~/Library/Logs/claude-instances/<Name>.log`. Every
  disappearance logs a snapshot: the instance's pid and uptime, Claude and
  staged-update versions, waiting ShipIt pids, which other instances are still
  running (these are what block an install), any Claude crash or jetsam reports
  since launch, and the relevant quit/update lines from Claude's own
  `~/Library/Logs/Claude/main.log` (shared by all instances). `beforeQuitForUpdate`
  there means the updater closed it.
- Opening an instance's launcher while its watchdog is waiting (for an update,
  or in the short grace window after a quit) reopens the instance right away.
- Quitting an instance at the exact moment an update installs looks like an
  update quit, so it may reopen once; quit it again and it stays quit.
- Launching an instance's launcher while the instance already runs never opens
  a second copy: it adopts the instance under watchdog protection (or defers to
  the watchdog that already has it). It won't raise the window; click the
  window itself for that.
- `new-claude --reopen-all` reopens any instance that isn't running; use it
  after upgrading launchers, or any time a collapse already happened.
- Watchdogs end at logout; launchers reopened by macOS "resume windows" at
  login restart them automatically.

---

## Instances TUI

`claude-instances` is a terminal dashboard for all your Claude launchers. Its
main job is usage: one table row per instance with its plan usage (5-hour and
weekly limits, each with its age), plus its state (running, watchdog) and
which account it is signed into. From the same table you can act on the
selected instance:

| Key | Action |
| --- | --- |
| `f` | bring to front |
| `l` | launch |
| `q` | quit |
| `w` | re-arm the watchdog (a fresh watchdog adopts the running instance) |
| `a` | launch all instances that aren't running, in table order |
| `r` | refresh usage now |
| `s` | sign in (see below) |
| `d` | details, including the exact rebuild command for the launcher |
| `o` | open the watchdog log |
| `m` | mark the current account as the expected one |
| `shift+up` / `shift+down` | move the row; the order is saved for `a` and `--launch-all` |
| `ctrl+q` | exit |

### Install

From the repo root:

```sh
pipx install .
```

For development, use a virtual environment instead:

```sh
pip install -e '.[dev]'
```

Python 3.11 or newer is required. `--apps DIR` (repeatable) points the TUI at
launcher folders other than `/Applications` and `~/Applications`.

### Live usage

Live usage numbers come from claude.ai using each instance's own session, which
Claude keeps encrypted with the "Claude Safe Storage" key from your Keychain.
The TUI never reads the Keychain itself: you hand it the key through a file
descriptor with `av`, so the key lives only in memory.

Store the key in av once:

```sh
security find-generic-password -s "Claude Safe Storage" -a "Claude Key" -w | av save --stdin CLAUDE_SAFE_STORAGE
```

Allow the TUI to receive it. Depending on how av treats the pipx wrapper
(a launcher script behind a symlink), one of these forms works:

```sh
av bless ~/.local/bin/claude-instances
# if av refuses the launcher or symlink:
av bless --endorse-launcher ~/.local/bin/claude-instances
```

Run it:

```sh
av inject --mode=fd +CLAUDE_SAFE_STORAGE:3 -- claude-instances --key-fd 3
```

Without `--key-fd` the TUI runs in cached-only mode: it shows the last saved
numbers with their age and says so in the header. Re-bless after every
`pipx install` or reinstall, because the wrapper is rewritten. The usage API
is unofficial and may change without notice.

### What is stored where

Everything lives in `~/.config/claude-instances/`:

```text
order.txt                row order, one display name per line
expected-accounts.json   the account each instance is expected to use
usage-cache.json         last usage numbers and their times only
signin-restore.txt       instances to relaunch after a sign-in
```

Nothing secret is written to disk: the key, decrypted cookies and raw API
responses stay in memory.

### Bring to front

`f` uses System Events, so your terminal needs Automation permission: grant it
in System Settings > Privacy & Security > Automation > (your terminal) >
System Events.

### Sign-in helper

`s` signs the selected instance in while it is the only Claude running: it
quits every other Claude (and stops their watchdogs) so the `claude://` login
link can only land in the right instance. When you finish or cancel, it
relaunches everything it quit. The list of instances to relaunch is written to
`signin-restore.txt` before anything is quit, so it survives a crash and the
TUI offers to relaunch them on its next start.

### Rebuild your launchers

Launchers built before the TUI don't know the argless-default rule (a Claude
started with no `--user-data-dir` belongs to the launcher whose data is the
default folder). Rebuild each one so `--reopen-all`, `--launch-all`, the
watchdog and the TUI apply it. Always pass its existing data folder (and apps
folder, if not `/Applications`); never rebuild with the name alone, because
that repoints the launcher to a new, empty data folder:

```sh
new-claude "<Name>" --dir "<last part of the data path that new-claude --list shows>" [--apps "<folder holding the .app>"]
```

For example, a launcher named "Claude Personal" whose data is
`~/Library/Application Support/Claude` is rebuilt with:

```sh
new-claude "Claude Personal" --dir Claude
```

The TUI's details view (`d`) shows the exact command for each launcher.

### Dock app

To launch everything from the Dock, build a one-line AppleScript app and drag
it to the Dock:

```sh
osacompile -o "/Applications/Claude All.app" -e "do shell script \"$HOME/.local/bin/new-claude --launch-all\""
```

The path is absolute because `do shell script` runs with a minimal `PATH`.
`--launch-all` scans both `/Applications` and `~/Applications`.

---

## ChatGPT instances

The same trick works for OpenAI's **ChatGPT desktop app** — it is a
Chromium-based build, so it honors `--user-data-dir` just like Claude. It
also needs a second knob: the app keeps chats, sessions, and auth in a
"Codex home" (default `~/.codex`) that `--user-data-dir` does not move, so
the launcher additionally gives each instance a private `CODEX_HOME` inside
its data folder. Without it, an instance signed into a different account
still shows the original account's chats.

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
- **Two stores, one folder.** Each instance owns a separate Chromium profile
  (cookies, web login) plus a separate `CODEX_HOME` at
  `<data folder>/codex-home` (chats, sessions, app auth). `--purge` wipes
  both. Launchers built before the `CODEX_HOME` fix shared `~/.codex`;
  re-run `new-chatgpt` with the same name to rebuild them.

Requires ChatGPT.app installed at `/Applications/ChatGPT.app`
(from https://chatgpt.com/download).

---

## Notes (Claude instances)

- **Both windows show as "Claude" in Cmd+Tab.** They launch the same real
  binary, but they're genuinely separate instances with separate data.
- **The launcher stays resident as a watchdog.** It has no Dock icon and just
  babysits its instance (see "Surviving auto-updates"). Killing the launcher
  process leaves the Claude window running, only unprotected.
- **Icons:** the launcher borrows Claude's icon if a loose `.icns` exists in its
  Resources. If this build keeps its icon in an asset catalog, the launcher gets
  a generic icon — harmless. Set a custom one via Finder → Get Info if you like.
- **Disk:** each instance bootstraps its own Cowork VM environment on first
  launch (~1–2 GB per instance).

---

## Requirements

- macOS (tested on 15.5, Apple Silicon)
- Claude desktop app installed at `/Applications/Claude.app`
  (from https://claude.ai/download)
- For ChatGPT instances: ChatGPT desktop app installed at
  `/Applications/ChatGPT.app` (from https://chatgpt.com/download)
- For the instances TUI: Python 3.11 or newer and
  [pipx](https://pipx.pypa.io/)

---

## License

MIT — see [LICENSE](LICENSE).

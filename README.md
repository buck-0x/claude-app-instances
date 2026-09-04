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

---

## Notes

- **Both windows show as "Claude" in Cmd+Tab.** They launch the same real
  binary, but they're genuinely separate instances with separate data.
- **The launcher is just a starter.** Once it opens a Claude window you can quit
  the launcher; the Claude window keeps running.
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

---

## License

MIT — see [LICENSE](LICENSE).

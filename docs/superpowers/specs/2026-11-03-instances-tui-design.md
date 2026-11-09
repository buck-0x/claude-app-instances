# Instances TUI: design

Date: 2026-11-03
Status: draft, awaiting approval (brainstormed 2026-10-30 to 2026-11-06)

## Intent

Running five Claude desktop instances means five identical-looking windows. Today
the only way to learn which ones are running, which account each is signed into,
and how close each is to its usage limits is to click through them one by one.
That caused real confusion: an instance (CCI) silently ended up signed into another
instance's account (Personal), showed an empty session list, and was mistaken for
the Personal window.

The TUI is one terminal screen that answers, at a glance: **is any instance close to
running out of usage?** It also shows what's running and signed in, catches account
mix-ups, and lets the user act on an instance (bring it forward, launch, quit, sign
in) without hunting for its window.

Success: the user opens the TUI, sees every instance's state and usage on one
screen, and can tell immediately if one is near a limit or on the wrong account.

## Goals

Per instance, shown in a single table:

1. **Running or not.**
2. **Signed in or not**, and **which account**.
3. **Usage:** 5-hour and weekly utilization, with reset times. Instances close to a
   limit are highlighted. This is the primary job.
4. **Launch order position**, editable from the TUI.

Actions:

5. **Bring to front:** raise that instance's window.
6. **Launch / quit** a single instance.
7. **Launch all in order.**
8. **Account mismatch warning.**
9. **Refresh usage now** for one instance.
10. **Details view:** account, data folder, launcher path, watchdog status, recent
    launcher log lines.
11. **Open watchdog log.**
12. **Sign-in helper:** sign one instance in without the login link landing in the
    wrong instance.

## Non-goals

These are tracked as GitHub issues, not part of this build:

- Creating, renaming or removing instances (wrapping `new-claude` / `remove-claude`).
- ChatGPT instances in the same screen.

Also out of scope: notifications or alerts when the TUI is closed (the user said
on-screen is enough), Linux/Windows support, and the Claude Code CLI's own login
(it is separate from the desktop instances and cannot report plan limits from a
script; checked on 2.1.314).

## Anti-goals

Any of these makes the build a failure even if it otherwise works:

- **Spawning a duplicate instance.** The repo already learned this the hard way
  (see `2026-09-11-update-watchdog-design.md` and the NOTE above `launch_instance`
  in `bin/new-claude.sh`): `open -a Claude` can start a stray argless instance, and
  Claude does not reliably hand off a second process on the same data dir. The TUI
  must never call `open -a` on Claude.app, never start Claude directly, and only
  launch instances via their launcher app (`open -n "<launcher>.app"`, as
  `--reopen-all` does), whose own lock prevents duplicates.
- **Secrets on disk or in output.** Decrypted cookies, the Safe Storage key and API
  responses containing account details are held in memory only: never written to
  files, logs or the terminal scrollback, and never committed. The repo is public.
- **Stale usage shown as current.** Every usage number carries its age. A number
  that is not from a live lookup in the last refresh cycle is visibly marked as
  cached and shows how old it is.
- **Writing into Claude's data folders.** All reads of instance data are read-only
  (SQLite opened `?immutable=1` / read-only, JSON read without writing back).

## Constraints

- macOS only, Apple Silicon and Intel. Claude.app is never modified.
- The repo is public and shared: pin versions with the ecosystem's own files
  (`pyproject.toml` `requires-python`), document a plain install in the README, and
  do not depend on mise.
- Personal data (emails, account/org UUIDs) must not appear in committed files,
  fixtures or test output. Test fixtures use made-up IDs.
- Existing behavior of `new-claude`, `remove-claude` and the watchdog launchers must
  not change except where this document says so.

## Decisions made with the user

- **Scope:** everything under Goals; the "bigger" items go to GitHub issues.
- **Alerts:** on screen only.
- **Usage source:** live lookup, with the Safe Storage key supplied through Automic
  Vault (`av`), falling back to cached numbers with their age when live fails.
- **Tech choice:** left to Claude (see below).

## Design

### Tech (Claude's call)

Python ≥ 3.11 with [Textual](https://textual.textualize.io/) for the UI and
`cryptography` for cookie decryption. Packaged with `pyproject.toml` and a console
entry point `claude-instances`. Install documented as `pipx install .` (or
`pip install -e .` for development). Lives in this repo next to `bin/`, e.g.
`tui/` or `src/claude_instances/` (builder's choice).

### Discovering instances

Same rule as `new-claude --list`, scanning both `/Applications` and
`~/Applications` (`new-claude --apps` can install elsewhere): every `*.app` whose
`CFBundleIdentifier` starts with `local.launcher.`. For each, read the data dir from
the launcher script (`DATA_DIR="..."` line, falling back to the inline
`--user-data-dir="..."` of old one-shot launchers) and the display name from
`CFBundleDisplayName`. `Claude.app` itself is not listed; the default data folder
appears through whichever launcher points at it (currently Claude Personal).

### Running

An instance is running when a process is the Claude main binary, has **no**
`--type=` argument (helpers carry `--user-data-dir` too), and has
`--user-data-dir=<data dir>` exactly. This is stricter than the zsh
`instance_pid()`, which doesn't exclude `--type=`. Compare real argv (e.g.
`psutil.Process.cmdline()`), or use the substring-plus-boundary check on the raw
`ps -axo pid=,command=` line. Never split the `ps` line on spaces: data dirs contain
`Application Support`. Keep the PID: bring to front and quit need it. If more than
one main process matches one data dir, show **DUPLICATE** on the row instead of
picking one.

**Argless Claude.** A main process with no `--user-data-dir` uses the default
folder (`~/Library/Application Support/Claude`). ShipIt relaunches Claude this way
after installing an update, and `open -a Claude` or a `claude://` link with no
Claude running can too. It counts as the running instance of whichever launcher
points at the default folder (currently Claude Personal), and the row says "not
started by a launcher".

**Change to the zsh scripts (exception to "existing behavior must not change"):**
`instance_running_for()` in `new-claude` and `instance_pid()` in the generated
launcher must apply the same argless rule. When the data dir is the default
folder, an argless main process counts as running. Without this, `--reopen-all`,
the new `--launch-all`, and the launcher itself would miss an argless default
Claude and start a second instance on the same folder. Existing launchers must be
rebuilt with `new-claude` to pick this up; the README says so.

### Signed in

Signed in when the instance's `<data dir>/Cookies` SQLite has a `.claude.ai`
`sessionKey` cookie that hasn't expired (`expires_utc` is Chromium time:
microseconds since 1601-01-01; `0` means a session cookie, not expired). No
decryption needed. Show the expiry date in details. (Checked 2026-11-06: Claude
keeps the live store at the data dir root, not `Network/Cookies`; fall back to
`Network/Cookies` if a future build moves it.) Open read-only (`?immutable=1` or
`Connection.deserialize` of the bytes, which avoids a temp copy) and retry on
`DatabaseError` if a read catches a commit in progress. Chromium flushes cookie
changes to disk in batches, so a new cookie can take ~30 s to appear.

Caveat to surface in the details view: a present cookie means the app believes it
is signed in; a server-side revocation only shows once the app next talks to the
server. When live lookup is available and the server rejects the cookie, show
"session rejected" instead of "signed in".

### Which account

`config.json` → `lastKnownAccountUuid` gives the account without decryption. With
live lookup available, also fetch the account's email for display. Without it, show
the short UUID (first 8 characters).

### Account mismatch warning

Two checks, both shown as a warning on the row:

1. **Shared account:** two or more **signed-in** instances on the same account
   (`lastKnownAccountUuid` survives sign-out, so signed-out rows are excluded and
   show "last: <short id>" instead). Needs
   no setup and cannot be silenced: the user confirmed they never run two
   instances on one account on purpose, so this is always a mistake. (This is
   exactly the CCI/Personal mix-up.)
2. **Not the expected account:** each instance can have an *expected* account,
   recorded when the user presses "this is the right account" on its row. If the
   current account differs, warn. Instances with no expected account set skip this
   check.

### Usage

**Live path.** For each signed-in instance:

1. Decrypt its `sessionKey` (and `lastActiveOrg`, to get the org) cookies.
   Chromium on macOS: key = PBKDF2-HMAC-SHA1(password, salt `saltysalt`, 1003
   iterations, 16 bytes); value is `v10` + AES-128-CBC with IV of 16 spaces; recent
   Chromium cookie DBs (meta table `version` ≥ 24) prefix the plaintext with the
   32-byte SHA-256 of the cookie row's `host_key` (e.g. `.claude.ai`), which must
   be stripped. If `lastActiveOrg` is missing, list the account's orgs instead
   (believed `GET /api/organizations`). claude.ai sits behind Cloudflare: send a
   browser-like User-Agent and treat a 403 challenge as "live unavailable", not
   "signed out". The password is the Keychain
   item service "Claude Safe Storage", account "Claude Key", shared by all
   instances.
2. Call claude.ai's usage endpoint for that org with the cookie. Believed to be
   `GET https://claude.ai/api/organizations/{org_uuid}/usage`, returning 5-hour and
   7-day utilization with reset times. **This API is unofficial and undocumented:
   the builder must confirm the endpoint and response shape against a real response
   before relying on it**, and handle it disappearing or changing.

   The user will be available during the build to store the key in `av` and run a
   live probe. So the build starts with a user-run probe script that prints only the
   response's structure (keys, value types, value ranges) and never emails, IDs or
   other account details, and the parser is built against that confirmed shape. The
   graceful-quit check against a real instance happens in the same session.

**Where the key comes from.** The TUI never reads the Keychain itself. The user
stores the Safe Storage password in Automic Vault once, and runs the TUI under
`av inject` (prefer `--mode=fd` so it arrives through a pipe rather than the
environment), with the script approved via `av bless`. The exact `av save` /
`av inject` invocation and secret name are the builder's to work out from
`av --help` and the Automic Vault docs; document them in the README. If the TUI is
started without the key, it runs in cached-only mode and says so in its header.
With pipx, the path to bless is the generated wrapper in `~/.local/bin`, which
changes on reinstall; the README says to re-bless after reinstalling.

**Keeping secrets out of crash output.** Textual prints uncaught exceptions as a
Rich traceback that includes local variables, so a crash in the lookup code could
print the key, a decrypted cookie or a raw response. Hold secrets in a small
wrapper type whose `__repr__`/`__str__` redact; catch every exception inside the
lookup layer and surface a one-line error on the row; never log request headers,
cookies or response bodies.

**Cached path.** Some instances have `plan-usage-history.json` in their data dir:
`{"version": 2, "samples": [{"t": <ms>, "org": "<uuid>", "u": {"fh": <5h %>, "sd":
<7d %>}}]}`. Use the latest sample for the instance's current org. Claude only
refreshes it when its usage panel has been opened in the last 24 hours, so it is
often weeks old.

**Which instances get live lookups** (my call): every signed-in instance, running
or not. Usage belongs to the account, not the window, and the main question is
whether *any* account is close to running out.

**TUI's own cache** (my call): after each successful live lookup, save only
`{display name: {five_hour_pct, five_hour_resets_at, week_pct, week_resets_at,
fetched_at}}` to `~/.config/claude-instances/usage-cache.json`.
Those are numbers and times only: no emails, cookies, org/account IDs or raw
responses. Each entry also stores a short SHA-256 prefix of the account UUID it was
fetched for, and is discarded if the instance is now on a different account, so an
account switch never shows the old account's numbers. On startup, and whenever a live lookup fails, show these numbers marked
with their age. Fall back to Claude's own `plan-usage-history.json` only when the
TUI cache has nothing newer.

**Display.** 5-hour % and weekly %, each with time until reset (live only).
Highlight at ≥ 80% (warning) and ≥ 95% (critical); my call, make them constants.
Cached numbers are dimmed and show their age ("44% · 37d old"). No data: "—".

**Refresh cadence** (my call): running / signed-in / account state every 10 s
(local reads only, cheap); live usage every 5 minutes per instance, plus the manual
"refresh now" action. Requests go out one at a time, not in parallel bursts. Back
off after errors.

### Launch order

Stored in `~/.config/claude-instances/order.txt`: one display
name per line. Plain text so `new-claude` (zsh) can read it without a JSON parser.
Instances not in the file go at the end, alphabetically. Moving a row up/down in the
TUI rewrites the file immediately.

Expected accounts live alongside in
`~/.config/claude-instances/expected-accounts.json`
(`{"<display name>": "<account uuid>"}`). This is on the user's machine, never in
the repo.

### Actions

- **Bring to front:** raise the window of the instance's PID through System Events
  (set `frontmost` of the process whose `unix id` is the PID). Never `open`, and
  never address Claude by app name (`tell application "Claude" to activate` can
  launch an argless Claude). Requires the terminal to have Automation permission
  for System Events; if missing, show a one-line explanation of how to grant it.
  Not running: say so and offer launch.
- **Launch:** `open -n "<launcher>.app"`. Already running: no-op with a message.
  A second launch of the same instance within 30 s, before it shows up as
  running, is refused (a second launcher would signal the watchdog to reopen the
  instance after a later Cmd+Q).
- **Quit:** after a y/n confirmation, first stop that instance's resident watchdog
  (same matching as `kill_watchdogs_for` in `bin/new-claude.sh`: only processes
  whose command line contains the launcher script path, never Claude itself), then
  ask the instance's main process to quit gracefully (SIGTERM, or whatever the
  builder verifies gives Claude a clean `beforeQuit`/`willQuit` shutdown; check the
  shared `main.log`). *My call:* stopping the watchdog first matters because when an
  update is staged the watchdog treats any quit as an update quit and reopens the
  instance later, sometimes hours later. A quit from the TUI must stay quit.
  Launching again via the launcher starts a fresh watchdog, so nothing is lost.
  If the process is still alive after ~30 s (Claude can refuse to quit, e.g. a
  running Cowork task or an open dialog), say "did not quit; watchdog stopped" and
  offer "re-arm watchdog" = `open -n "<launcher>.app"`, which adopts the running
  instance without starting another. Note: if an update is staged, ShipIt may
  install it and relaunch the default Claude argless afterwards, so the default
  folder's row can reappear as "not started by a launcher"; that is expected.
- **Launch all in order:** walk the order list, skip running instances, launch the
  rest via their launchers with a 3 s gap (matching today's Claude All app).
  Also add the same behavior to `new-claude` as `--launch-all` so the Dock app can
  use it without the TUI. Unlike `--list` and `--reopen-all`, `--launch-all` scans
  both `/Applications` and `~/Applications` unless `--apps` is given, matching the
  TUI.
- **Older launchers:** every launch, re-arm, launch-all and restore re-checks the
  process list just before `open -n`. The TUI refuses to launch through an old
  one-shot launcher (it has no lock), and through a launcher built before the
  argless rule if it points at the default folder while an argless Claude is
  running. In both cases it says to rebuild that launcher.
- **Refresh usage now:** re-run the live lookup for that row immediately.
- **Details:** a panel showing display name, account (email or short id), expected
  account, data folder, launcher path, session cookie expiry, watchdog status
  (alive only if the PID in the lock in `~/Library/Logs/claude-instances/` is alive
  **and** its command line contains that launcher's script path, since the lock
  can go stale and PIDs get reused), and the last
  ~20 lines of `~/Library/Logs/claude-instances/<name>.log`.
- **Open watchdog log:** open that log in a pager (suspend the TUI, run `$PAGER` or
  `less`, resume).
- **Mark expected account:** record the current account as this instance's expected
  one.

### Sign-in helper

Problem it solves: a login link opens through the `claude://` URL scheme, and macOS
hands it to whichever Claude process it picks. With several instances running, the
login can land in the wrong one (the likely cause of the CCI mix-up).

Flow:

1. Show which instances will be quit and ask for confirmation.
2. Write the restore list (display names of running instances other than the
   target) to `~/.config/claude-instances/signin-restore.txt` **before** quitting
   anything.
3. Quit every running instance except the target (stopping each one's watchdog
   first, as in Quit, so none comes back during the login). An argless Claude is
   quit only if the target is not the default-folder instance; if the target is
   the default folder, the argless process *is* the target. Also quit Claude
   processes on folders no launcher points at, both processes of a duplicate row,
   and instances behind old one-shot launchers; these are listed as "not
   restorable" in the step 1 confirmation and are not relaunched. Also stop the
   watchdogs of instances that aren't running but are waiting on an update (so
   none reopens mid-login); those instances go on the restore list. Refuse to
   start if the target itself is a duplicate. Wait until they're gone (timeout
   with a clear message). An instance that refuses to quit is re-armed (its
   watchdog restarted) rather than left unprotected, and the flow fails.
4. Launch the target via its launcher if it isn't running, and wait until the
   Running rule confirms it is up. Do not show the next step before then: a login
   link opened with no Claude running starts an argless Claude and the login lands
   in the default folder. Then require that the target is the **only** Claude
   running: anything that appeared meanwhile (e.g. an argless Claude ShipIt
   relaunched after installing an update) is quit, added to the restore list, and
   reported. A refused target launch fails the flow immediately.
5. Show "Sign in now in <name>. Waiting..." with a cancel key.
6. Done when the target's account changes or a fresh `sessionKey` cookie appears
   (allow for the ~30 s cookie flush); the builder picks the most reliable signal.
7. Offer to record the new account as the expected one.
8. Relaunch the instances on the restore list, in launch order, via their
   launchers. A quit argless default Claude is relaunched through the
   default-folder launcher if one exists; otherwise say it was not relaunched.
   A relaunch the launcher refuses is reported and stays in the restore file;
   otherwise delete the restore file.

Steps 3 to 8 run inside try/finally. Ctrl+C and Ctrl+Q (which Textual delivers as
key presses, not signals) and SIGTERM/SIGHUP all cancel the flow and wait for the
restore before the TUI exits, so cancel, an exception or exiting all relaunch what
was quit. If the TUI dies anyway (terminal
closed, kill -9), the restore file survives: on next start, the TUI offers
"Relaunch N instances quit by an interrupted sign-in". The restore file records
the owning TUI's PID, and the offer appears only when that process is gone, so a
second TUI can't restore instances in the middle of another TUI's sign-in.

### Dock app

Today's `/Applications/Claude All.app` (built locally with `osacompile`) has a
hard-coded list. After this work it should just run `new-claude --launch-all`,
called by absolute path (`do shell script` runs with a minimal `PATH` that doesn't
include `~/.local/bin`). The README documents the `osacompile` one-liner; the
builder rebuilds the user's local copy once.

### Layout sketch

```
 Claude instances                         live usage ✓   refreshed 16:42:10
 #  Instance              State     Account        5h          Week
 1  Claude Personal       ● open    me@…           12%  2h10m  44%  3d
 2  Claude P2 Bucky Zero  ● open    p2@…           81%  0h40m  52%  5d
 3  Claude CCI            ● open    ⚠ me@… (shared with Personal)
 4  Claude V12            ○ closed  v12@…          30% · 2h old
 5  Claude Fulcra         ● open    signed out     —           —
 f front  l launch  q quit  a launch all  r refresh  s sign in  d details  ↑↓ move
```

Exact layout, colors and keybindings are the builder's.

## Left to the builder

- Package layout, module structure, and how much of the instance-detection logic
  is shared with the zsh scripts versus reimplemented in Python.
- Exact keybindings, colors and layout.
- Confirming the usage endpoint, its response shape, and how to get the email.
- The graceful-quit mechanism, after verifying it.
- The `av` invocation and secret name.
- Test approach. Pure logic (instance discovery, running-process matching, cookie
  expiry parsing, cached-usage parsing, order file handling, mismatch detection)
  should have unit tests against fixture data dirs with fake IDs. The live API and
  System Events calls sit behind small interfaces so they can be faked.

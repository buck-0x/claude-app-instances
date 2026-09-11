# Update-proof instance launchers (watchdog) — design

Date: 2026-09-11
Status: approved (brainstormed 2026-09-09)

## Problem

Every instance launcher runs the same `/Applications/Claude.app` bundle, only with a
different `--user-data-dir`. Claude's auto-updater quits each running instance
(a "stealth relaunch") so its Squirrel/ShipIt helper can swap the bundle, and the
relaunch does not reliably carry `--user-data-dir` for non-default instances. Result:
after an update, extra instances collapse until only one is left.

Evidence gathered on 2026-09-08: `[stealth-relaunch]` and `update_relaunch_marker`
entries in `~/Library/Logs/Claude/main.log`, a staged update in
`~/Library/Caches/com.anthropic.claudefordesktop.ShipIt/`, clean `willQuit` shutdowns,
and zero Claude crash reports; the instances are deliberately quit, not crashing.

The current launcher is `exec open -n ...` and exits immediately, so nothing exists to
bring an instance back.

## Approach chosen

Self-watching launcher (approved over a launchd daemon and over a manual-only
command): the launcher app stays resident as a tiny shell watchdog for its instance.
A manual `new-claude --reopen-all` fallback command is also included.

## Design

### Watchdog launcher

`new-claude` keeps generating the same `.app` structure, but
`Contents/MacOS/launcher` becomes a watch loop:

1. **Single-watchdog lock**: an atomic symlink lock (`ln -s $$`) per instance under
   `~/Library/Logs/claude-instances/`. If a live watchdog already holds it, the new
   launcher exits. Stale locks (dead pid) are reclaimed. No path in the launcher may
   risk spawning a duplicate instance; "focus the window" helpers were dropped for
   this reason (verified in practice: Claude does not reliably hand off a second
   same-data-dir process via a single-instance lock, and `open -a` can spawn an
   argless instance).
2. **Adopt or launch**: if an instance with our exact `--user-data-dir` is already
   running, adopt it (start babysitting). Otherwise launch it with
   `open -n -a Claude.app --args --user-data-dir=<dir>` and record Claude.app's
   `CFBundleVersion`.
3. **Babysit by polling** (`ps` for the main Claude binary plus our exact data-dir
   argument, every ~20s). Polling rather than `open -W` so the watchdog also survives
   the case where Claude successfully relaunches itself; it simply keeps watching the
   new process.
4. **On disappearance**: wait a ~20s grace period, then re-check:
   - instance came back on its own: keep babysitting (refresh recorded version)
   - gone and bundle version changed since last launch: update-driven quit; relaunch
     (one retry), record the new version, keep babysitting
   - gone and version unchanged: treated as a user quit (or a crash); watchdog exits
5. **Loop bound**: at most one auto-relaunch per bundle version; a second death on the
   same version exits the watchdog instead of thrashing.

Supporting details:

- `LSUIElement=true` on the launcher bundle so the resident watchdog holds no Dock
  icon (Claude itself still appears in the Dock).
- Per-instance log at `~/Library/Logs/claude-instances/<Name>.log`, size-capped.
- Poll/grace/settle intervals overridable via `CLAUDE_LAUNCHER_POLL_SECS`,
  `CLAUDE_LAUNCHER_GRACE_SECS`, `CLAUDE_LAUNCHER_SETTLE_SECS` (test hooks; `open`
  does not propagate shell env, so real launches always use the defaults).

Accepted tradeoffs: quitting an instance at the exact moment an update installs looks
like an update quit and may reopen it once (quit again and it stays quit); crashes
without a version change do not relaunch.

### `new-claude --reopen-all`

Reuses `--list` discovery (bundle id `local.launcher.*`; ChatGPT launchers use
`local.launcher-chatgpt.*` and are not matched). For each launcher whose instance is
not running, `open` the launcher app; its own guards handle the rest. Covers "the
collapse already happened" and pre-watchdog launchers.

### `remove-claude` and rebuilds

Both `remove-claude` and a `new-claude` rebuild kill any running watchdog for that
launcher path before deleting it, so a live watchdog cannot resurrect a removed
instance or double up after a rebuild. The running Claude instance itself is left
alone.

### Testability

Hidden `new-claude --claude-app <path>` override (default `/Applications/Claude.app`).
Tests build a launcher against a stub app bundle whose binary just sleeps, then kill
the stub with and without bumping the stub's `CFBundleVersion` and assert
relaunch/no-relaunch, plus the lock and reopen-all behaviors. Signed Claude is never
touched.

### Migration

Re-running `new-claude "Name"` rebuilds a launcher in place; data dirs, logins,
extensions and history are untouched. Each instance must be quit and reopened from
its rebuilt launcher once so the watchdog is the thing that started it (or adopted
via `--reopen-all` while running).

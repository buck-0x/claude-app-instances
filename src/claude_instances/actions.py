"""System actions: bring to front, guarded launch, watchdog-first quit, launch all.

Two mistakes matter most here. Opening a launcher while its instance already runs
can start a duplicate Claude on the same data folder, so every launch goes through
`guarded_launch`, which re-reads `ps` just before `open -n` and refuses when that
read fails. Quitting an instance while its watchdog lives lets the watchdog reopen
it later, so `quit_instance` stops the watchdog first and quits only once a `ps`
read confirms the watchdog is gone.

Everything that touches the machine sits behind `SystemActions`, so tests use a fake.
"""

import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from claude_instances import paths
from claude_instances.launchers import Launcher
from claude_instances.processes import (
    MainProcess,
    ProcessLine,
    claude_binary,
    list_processes,
    main_processes,
    match_instances,
    watchdog_pids,
)
from claude_instances.snapshot import InstanceState, Snapshot

LAUNCH_GAP_S = 3
QUIT_TIMEOUT_S = 30
WATCHDOG_STOP_TIMEOUT_S = 5

# The signal that asks Claude to quit gracefully. Provisional until Checkpoint A
# confirms SIGTERM gives a clean beforeQuit/willQuit shutdown; change it only here.
QUIT_SIGNAL = signal.SIGTERM

WATCHDOG_POLL_S = 0.5
WATCHDOG_KILL_WAIT_S = 2
QUIT_POLL_S = 1
FRONT_TIMEOUT_S = 10

AUTOMATION_HELP = (
    "Allow your terminal to control System Events: System Settings > Privacy & Security"
    " > Automation > <terminal> > System Events"
)
_AUTOMATION_DENIED_MARKERS = ("-1743", "not allowed assistive access", "Not authorized")

# How a failed process-list read surfaces: `ps` exiting non-zero or not starting.
_PS_ERRORS = (subprocess.SubprocessError, OSError)
PS_UNREADABLE = "could not read the process list"
# Ends the message for a launch refused because one was requested moments ago.
LAUNCH_PENDING = "waiting for it to appear"
# launch_all's skip for a row whose watchdog reopens it once a Claude update installs.
# Launching it would reopen it on the old version and keep blocking the install.
UPDATE_WAIT_SKIP = "waiting for update; skipped"


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str


class SystemActions(Protocol):
    def bring_to_front(self, pid: int) -> ActionResult: ...
    def launch(self, app_path: Path) -> ActionResult: ...
    def send_quit(self, pid: int) -> ActionResult: ...
    def signal_pids(self, pids: list[int], sig: int) -> None: ...
    def is_alive(self, pid: int) -> bool: ...
    def processes(self) -> list[ProcessLine]: ...
    def remove_lock_if_names(self, lock: Path, pid: int) -> bool: ...


def remove_lock_if_names(lock: Path, pid: int) -> bool:
    """Remove the watchdog lock symlink `lock` only if it points at `pid`."""
    try:
        if os.readlink(lock) != str(pid):
            return False
        os.unlink(lock)
    except OSError:
        return False
    return True


def automation_denied(stderr: str) -> bool:
    """True when osascript failed because the terminal lacks Automation permission."""
    return any(marker in stderr for marker in _AUTOMATION_DENIED_MARKERS)


class MacSystemActions(SystemActions):
    """The real machine. Never addresses Claude by app name and never starts it directly."""

    def launch_argv(self, app_path: Path) -> list[str]:
        return ["open", "-n", str(app_path)]

    def front_argv(self, pid: int) -> list[str]:
        return [
            "osascript", "-e",
            f'tell application "System Events" to set frontmost of'
            f' (first process whose unix id is {pid}) to true',
        ]

    def bring_to_front(self, pid: int) -> ActionResult:
        try:
            result = subprocess.run(self.front_argv(pid), capture_output=True, text=True,
                                    errors="replace", timeout=FRONT_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as e:
            return ActionResult(False, f"could not bring to front: {e}")
        if result.returncode == 0:
            return ActionResult(True, "brought to front")
        if automation_denied(result.stderr):
            return ActionResult(False, AUTOMATION_HELP)
        return ActionResult(False, f"could not bring to front: {result.stderr.strip()}")

    def launch(self, app_path: Path) -> ActionResult:
        try:
            result = subprocess.run(self.launch_argv(app_path), capture_output=True,
                                    text=True, errors="replace")
        except OSError as e:
            return ActionResult(False, f"could not open launcher: {e}")
        if result.returncode != 0:
            return ActionResult(False, f"could not open launcher: {result.stderr.strip()}")
        return ActionResult(True, "launched")

    def send_quit(self, pid: int) -> ActionResult:
        try:
            os.kill(pid, QUIT_SIGNAL)
        except ProcessLookupError:
            return ActionResult(True, "already gone")
        except PermissionError as e:
            return ActionResult(False, f"could not ask {pid} to quit: {e}")
        return ActionResult(True, "asked to quit")

    def signal_pids(self, pids: list[int], sig: int) -> None:
        for pid in pids:
            try:
                os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass

    def is_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by someone else
        return True

    def processes(self) -> list[ProcessLine]:
        return list_processes()

    def remove_lock_if_names(self, lock: Path, pid: int) -> bool:
        return remove_lock_if_names(lock, pid)


# ---- guarded launch -------------------------------------------------------------

def launch_refusal(
    launcher: Launcher, procs: list[MainProcess], default_data_dir: Path, rearm: bool = False
) -> str | None:
    """Why opening `launcher` now could start a duplicate, or None when it is safe.

    `rearm` allows opening while the instance runs: a watchdog launcher then adopts
    the running instance instead of starting another.
    """
    if not launcher.is_watchdog:
        return "old one-shot launcher; rebuild it with new-claude before launching from here"
    if not rearm:
        matched, _ = match_instances(procs, [launcher], default_data_dir)
        if matched[launcher.name]:
            return "already running"
    if (launcher.data_dir == default_data_dir and not launcher.has_argless_rule
            and any(p.data_dir is None for p in procs)):
        return ("launcher predates the argless rule and an argless Claude is running on"
                " its folder; rebuild it first")
    return None


def guarded_launch(
    launcher: Launcher, actions: SystemActions, default_data_dir: Path, rearm: bool = False
) -> ActionResult:
    """Open `launcher` with `open -n` only if a fresh `ps` read shows it is safe.

    A failed read refuses: it means "cannot verify", never "nothing running".
    """
    try:
        lines = actions.processes()
    except _PS_ERRORS:
        return ActionResult(False, f"{PS_UNREADABLE}; not launching")
    procs = main_processes(lines, claude_binary(launcher.claude_app))
    reason = launch_refusal(launcher, procs, default_data_dir, rearm)
    if reason is not None:
        return ActionResult(False, reason)
    return actions.launch(launcher.app_path)


# ---- quit -----------------------------------------------------------------------

def _wait_gone(
    poll: Callable[[], list[int]], sleep: Callable[[float], None],
    interval: float, timeout_s: float,
) -> list[int]:
    """Poll at once, then every `interval` until nothing is left or `timeout_s` passes."""
    waited = 0.0
    while True:
        left = poll()
        if not left or waited >= timeout_s:
            return left
        sleep(interval)
        waited += interval


def _stop_watchdogs(
    script_paths: list[Path],
    actions: SystemActions,
    sleep: Callable[[float], None],
    timeout_s: float,
    locks: list[Path],
) -> tuple[list[int], list[int]]:
    """`stop_watchdogs`, also returning the watchdog pids it found: (found, survivors)."""

    def remaining() -> list[int]:
        lines = actions.processes()
        return sorted({pid for script in script_paths for pid in watchdog_pids(lines, script)})

    found = remaining()
    if not found:
        return [], []

    def still_running() -> list[int]:
        return [pid for pid in remaining() if pid in found]

    actions.signal_pids(found, signal.SIGTERM)
    left = _wait_gone(still_running, sleep, WATCHDOG_POLL_S, timeout_s)
    if not left:
        return found, []
    actions.signal_pids(left, signal.SIGKILL)
    survivors = _wait_gone(still_running, sleep, WATCHDOG_POLL_S, WATCHDOG_KILL_WAIT_S)
    # SIGKILL skips the watchdog's EXIT trap, so its lock still names the dead pid; a
    # later launcher could take a reused pid for a live watchdog. Remove only a lock
    # that names a pid we killed and saw vanish.
    for pid in left:
        if pid not in survivors:
            for lock in locks:
                actions.remove_lock_if_names(lock, pid)
    return found, survivors


def stop_watchdogs(
    script_paths: list[Path],
    actions: SystemActions,
    sleep: Callable[[float], None] = time.sleep,
    timeout_s: int = WATCHDOG_STOP_TIMEOUT_S,
    locks: list[Path] | None = None,
) -> list[int]:
    """Stop every process whose command line contains one of `script_paths`.

    SIGTERM, wait up to `timeout_s`, then SIGKILL survivors and wait 2 s more.
    Returns the pids still alive. After a SIGKILL, each of `locks` that still names
    a killed pid is removed. Claude itself is never touched, since its command line
    never contains a launcher script path. Raises if `ps` cannot be read.

    zsh runs a TERM trap only once its foreground command ends, and the watchdog
    sleeps up to 20 s after a launch, so the SIGKILL path is reachable in normal use.
    """
    return _stop_watchdogs(script_paths, actions, sleep, timeout_s, locks or [])[1]


class QuitRequestFailed(Exception):
    """A quit request could not be delivered (for example, permission denied)."""


def quit_pids(
    pids: list[int],
    actions: SystemActions,
    sleep: Callable[[float], None] = time.sleep,
    timeout_s: int = QUIT_TIMEOUT_S,
) -> list[int]:
    """Ask each pid to quit, then poll every 1 s up to `timeout_s`; return survivors.

    Raises `QuitRequestFailed` without polling when any request fails, so the caller
    reports the real reason instead of a timeout 30 s later.
    """
    failures = [r.message for r in (actions.send_quit(pid) for pid in pids) if not r.ok]
    if failures:
        raise QuitRequestFailed("; ".join(failures))
    return _wait_gone(lambda: [p for p in pids if actions.is_alive(p)],
                      sleep, QUIT_POLL_S, timeout_s)


class QuitOutcome(Enum):
    QUIT = "quit"
    DID_NOT_QUIT = "did not quit"
    WATCHDOG_STUCK = "watchdog stuck"
    NOT_RUNNING = "not running"


def _still_this_instance(
    state: InstanceState, lines: list[ProcessLine], default_data_dir: Path
) -> list[int]:
    """The snapshot pids that `lines` still show as this launcher's Claude mains."""
    launcher = state.launcher
    procs = main_processes(lines, claude_binary(launcher.claude_app))
    matched, _ = match_instances(procs, [launcher], default_data_dir)
    current = {r.pid for r in matched[launcher.name]}
    return [pid for pid in state.pids if pid in current]


def quit_instance(
    state: InstanceState,
    actions: SystemActions,
    sleep: Callable[[float], None] = time.sleep,
    default_data_dir: Path | None = None,
    log_dir: Path | None = None,
) -> tuple[QuitOutcome, str]:
    """Stop the instance's watchdog, and only once it is confirmed gone, quit the instance.

    If the watchdog cannot be stopped, or `ps` cannot be read to confirm it, the
    instance is left running: a quit with a live watchdog would not stay quit. The
    quit goes only to snapshot pids that a fresh `ps` read still shows as this
    instance, so a reused pid is never signalled. A row that is not running but has
    a watchdog waiting to reopen it gets that watchdog stopped.
    `default_data_dir` and `log_dir` default to `paths.default_data_dir()` and
    `paths.log_dir()`.
    """
    if default_data_dir is None:
        default_data_dir = paths.default_data_dir()
    if log_dir is None:
        log_dir = paths.log_dir()
    lock = log_dir / f"{state.name}.lock"
    try:
        found, stuck = _stop_watchdogs([state.launcher.script_path], actions, sleep,
                                       WATCHDOG_STOP_TIMEOUT_S, [lock])
    except _PS_ERRORS:
        return (QuitOutcome.WATCHDOG_STUCK,
                f"{PS_UNREADABLE} to stop the watchdog; instance left running")
    if stuck:
        return QuitOutcome.WATCHDOG_STUCK, "watchdog did not stop; instance left running"
    if not state.pids:
        if found:
            return QuitOutcome.NOT_RUNNING, f"stopped watchdog waiting to reopen {state.name}"
        return QuitOutcome.NOT_RUNNING, "not running"
    try:
        pids = _still_this_instance(state, actions.processes(), default_data_dir)
    except _PS_ERRORS:
        return (QuitOutcome.DID_NOT_QUIT,
                f"{PS_UNREADABLE}; watchdog stopped, instance left running")
    if not pids:
        return QuitOutcome.QUIT, "already quit; watchdog stopped"
    try:
        survivors = quit_pids(pids, actions, sleep)
    except QuitRequestFailed as e:
        return QuitOutcome.DID_NOT_QUIT, f"could not ask it to quit ({e}); watchdog stopped"
    if survivors:
        return QuitOutcome.DID_NOT_QUIT, "did not quit; watchdog stopped"
    return QuitOutcome.QUIT, "quit"


# ---- launch all -------------------------------------------------------------------

def launch_all(
    snapshot: Snapshot,
    actions: SystemActions,
    default_data_dir: Path,
    sleep: Callable[[float], None] = time.sleep,
    gap_s: int = LAUNCH_GAP_S,
    launch_one: Callable[[Launcher], ActionResult] | None = None,
) -> list[tuple[str, ActionResult]]:
    """Launch every row the snapshot shows closed, in row order.

    Rows shown running, duplicated or waiting for an update to install are reported
    as skipped. After a successful
    launch the next attempt waits `gap_s` first; there is no wait after the last row.
    Each launch still goes through `launch_one` (default `guarded_launch`), which
    re-reads `ps`, so a stale snapshot cannot cause a duplicate.
    """
    if launch_one is None:
        def launch_one(launcher: Launcher) -> ActionResult:
            return guarded_launch(launcher, actions, default_data_dir)

    results: list[tuple[str, ActionResult]] = []
    gap_due = False
    for row in snapshot.rows:
        if row.is_duplicate:
            results.append((row.name, ActionResult(False, "duplicates running; skipped")))
            continue
        if row.is_running:
            results.append((row.name, ActionResult(False, "already running")))
            continue
        if row.update_waiting:
            results.append((row.name, ActionResult(False, UPDATE_WAIT_SKIP)))
            continue
        if gap_due:
            sleep(gap_s)
            gap_due = False
        result = launch_one(row.launcher)
        results.append((row.name, result))
        gap_due = result.ok
    return results

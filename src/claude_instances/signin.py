"""Sign-in helper: sign one instance in while it is the only Claude running.

A login link opens through the `claude://` URL scheme and macOS hands it to whichever
Claude process it picks, so the flow quits every other Claude first, waits for the
login in the target, then relaunches what it quit. The list of instances to relaunch
is written to the restore file before anything is quit, and the relaunch runs in a
`finally`, so cancel, a failure or an exception all restore. A relaunch the launcher
refuses stays in the restore file, so a later TUI can offer it again.
"""

import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from claude_instances import paths
from claude_instances.actions import (
    LAUNCH_GAP_S,
    LAUNCH_PENDING,
    ActionResult,
    QuitRequestFailed,
    SystemActions,
    guarded_launch,
    quit_pids,
    stop_watchdogs,
)
from claude_instances.launchers import Launcher
from claude_instances.paths import RESTORE_FILE
from claude_instances.processes import (
    MainProcess,
    claude_binary,
    main_processes,
    match_instances,
    watchdog_pids,
)
from claude_instances.snapshot import InstanceState, Snapshot

LAUNCH_TIMEOUT_S = 60
POLL_S = 2
# How many times ISOLATING quits newly started Claudes before giving up.
ISOLATE_ROUNDS = 5
# Before relaunching, how long to wait for processes already asked to quit to exit, so
# the restore never re-arms a dying instance.
RESTORE_SETTLE_S = 10
SETTLE_POLL_S = 1

_OWNER_PREFIX = "owner "


class SigninPhase(Enum):
    QUITTING = "quitting"
    LAUNCHING = "launching"
    ISOLATING = "isolating"
    WAITING_FOR_LOGIN = "waiting for login"
    RESTORING = "restoring"
    DONE = "done"
    FAILED = "failed"


class SigninRefused(Exception):
    """The sign-in cannot start for this target; the message says why."""


@dataclass
class SigninPlan:
    target: str
    quit_rows: list[str]
    quit_pids: list[int]
    watchdog_rows: list[str]
    restore: list[str]
    not_restorable: list[str]
    # The snapshot the plan was made from: launchers, positions and pid labels.
    snapshot: Snapshot = field(repr=False, compare=False)


@dataclass
class SigninResult:
    ok: bool
    message: str
    new_account: str | None = field(default=None, repr=False)
    restored: list[str] = field(default_factory=list)
    restore_failed: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class _Failed(Exception):
    """Ends the sign-in early with a message for the user; the restore still runs."""


class _Cancelled(Exception):
    pass


def _notify(on_phase: Callable[[SigninPhase, str], None], phase: SigninPhase,
            message: str) -> None:
    """Report a phase; a failing UI callback (say, during TUI shutdown) never stops the flow."""
    try:
        on_phase(phase, message)
    except Exception:
        pass


def _stray_label(proc: MainProcess) -> str:
    return "argless Claude" if proc.data_dir is None else proc.data_dir.name


def _pid_labels(snapshot: Snapshot) -> dict[int, str]:
    """Row name per row pid, and `<description> (pid N)` per stray pid."""
    labels = {pid: r.name for r in snapshot.rows for pid in r.pids}
    for proc in snapshot.strays:
        labels.setdefault(proc.pid, f"{_stray_label(proc)} (pid {proc.pid})")
    return labels


def _restorable(row: InstanceState) -> bool:
    return not row.is_duplicate and row.launcher.is_watchdog


def plan_signin(snapshot: Snapshot, target: str) -> SigninPlan:
    """What signing `target` in will quit, stop and relaunch. Raises `SigninRefused`."""
    t = snapshot.row(target)
    if t.is_duplicate:
        raise SigninRefused("target has more than one process on its folder; quit one first")
    if not t.is_running and not t.launcher.is_watchdog:
        raise SigninRefused("old one-shot launcher; rebuild it with new-claude first")
    others = [r for r in snapshot.rows if r.name != target]
    quit_rows = [r for r in others if r.pids]
    watchdog_rows = [r for r in others if r.watchdog_alive]
    pids = [pid for r in quit_rows for pid in r.pids]
    pids += [p.pid for p in snapshot.strays if p.pid not in pids and p.pid not in t.pids]
    involved = {r.name for r in quit_rows} | {r.name for r in watchdog_rows}
    restore = [r.name for r in others if r.name in involved and _restorable(r)]
    not_restorable = []
    for r in quit_rows:
        if r.is_duplicate:
            not_restorable.append(f"{r.name} (duplicate; pids {', '.join(map(str, r.pids))})")
        elif not r.launcher.is_watchdog:
            not_restorable.append(f"{r.name} (old one-shot launcher)")
    not_restorable += [f"{_stray_label(p)} (pid {p.pid})" for p in snapshot.strays
                       if p.pid not in t.pids]
    return SigninPlan(
        target=target,
        quit_rows=[r.name for r in quit_rows],
        quit_pids=pids,
        watchdog_rows=[r.name for r in watchdog_rows],
        restore=restore,
        not_restorable=not_restorable,
        snapshot=snapshot,
    )


# ---- restore file -----------------------------------------------------------------

def write_restore(path: Path, names: list[str], owner_pid: int | None = None) -> None:
    """Write `owner <pid>` and then one name per line, atomically.

    `owner_pid` defaults to this process, so a second TUI can tell whether the
    sign-in that wrote the file is still running.
    """
    if owner_pid is None:
        owner_pid = os.getpid()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{_OWNER_PREFIX}{owner_pid}\n")
            f.write("".join(f"{name}\n" for name in names))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_restore(path: Path) -> tuple[int | None, list[str]]:
    """(owner pid, names); a missing file gives (None, []), a missing owner line None."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, []
    lines = [line for line in text.splitlines() if line.strip()]
    owner = None
    if lines and lines[0].startswith(_OWNER_PREFIX):
        try:
            owner = int(lines[0][len(_OWNER_PREFIX):])
            lines = lines[1:]
        except ValueError:
            pass
    return owner, lines


def clear_restore(path: Path) -> None:
    path.unlink(missing_ok=True)


# ---- flow -------------------------------------------------------------------------

class SigninFlow:
    """Runs one sign-in (spec steps 2 to 8). `run` blocks; call it off the UI thread."""

    def __init__(
        self,
        actions: SystemActions,
        refresh: Callable[[], Snapshot],
        config_dir: Path,
        default_data_dir: Path,
        sleep: Callable[[float], None] = time.sleep,
        on_phase: Callable[[SigninPhase, str], None] = lambda p, m: None,
        log_dir: Path | None = None,
        launch_one: Callable[[Launcher], ActionResult] | None = None,
        restore_one: Callable[[Launcher, bool], ActionResult] | None = None,
    ) -> None:
        """`launch_one` opens the target's launcher; the backend passes its own, which
        refuses a second open within its pending-launch window. `restore_one(launcher,
        rearm)` relaunches a restore-list row; the backend's records the launch as
        pending. Both default to `guarded_launch`."""
        self.actions = actions
        self.refresh = refresh
        self.restore_path = config_dir / RESTORE_FILE
        self.default_data_dir = default_data_dir
        self.sleep = sleep
        self.on_phase = on_phase
        self.log_dir = paths.log_dir() if log_dir is None else log_dir
        self.launch_one = launch_one if launch_one is not None else self._guarded_launch
        self.restore_one = restore_one if restore_one is not None else self._guarded_launch

    def _guarded_launch(self, launcher: Launcher, rearm: bool = False) -> ActionResult:
        return guarded_launch(launcher, self.actions, self.default_data_dir, rearm=rearm)

    def run(self, plan: SigninPlan, cancelled: threading.Event) -> SigninResult:
        try:
            write_restore(self.restore_path, plan.restore)
        except OSError as e:
            message = f"could not write the restore file ({e.strerror or type(e).__name__})"
            _notify(self.on_phase, SigninPhase.FAILED, message)
            return SigninResult(False, message)
        run = _Run(self, plan, cancelled)
        result = SigninResult(False, "sign-in failed")
        try:
            result.new_account = run.sign_in()
            result.ok, result.message = True, "signed in"
        except _Cancelled:
            result.message = "cancelled"
        except _Failed as e:
            result.message = str(e)
        except Exception as e:
            result.message = f"sign-in failed ({type(e).__name__})"
        finally:
            result.restored, result.restore_failed = run.restore()
            result.warnings = run.warnings
        phase = SigninPhase.DONE if result.ok else SigninPhase.FAILED
        _notify(self.on_phase, phase, result.message)
        return result


class _Run:
    """The state of one `SigninFlow.run`."""

    def __init__(self, flow: SigninFlow, plan: SigninPlan, cancelled: threading.Event) -> None:
        self.flow = flow
        self.actions = flow.actions
        self.plan = plan
        self.cancelled = cancelled
        self.target = plan.target
        self.launchers: dict[str, Launcher] = {}
        self.positions: dict[str, int] = {}
        self._learn(plan.snapshot)
        self.restore_list = list(plan.restore)
        self.warnings: list[str] = []
        self.asked_to_quit: set[int] = set()
        self.refused_to_quit: set[int] = set()

    def _learn(self, snapshot: Snapshot) -> None:
        for r in snapshot.rows:
            self.launchers[r.name] = r.launcher
            self.positions[r.name] = r.position

    def _phase(self, phase: SigninPhase, message: str) -> None:
        _notify(self.flow.on_phase, phase, message)

    def _check_cancel(self) -> None:
        if self.cancelled.is_set():
            raise _Cancelled()

    def _refresh(self) -> Snapshot:
        snapshot = self.flow.refresh()
        self._learn(snapshot)
        return snapshot

    # -- quitting --

    def _stop_watchdogs(self, names: list[str]) -> None:
        launchers = [self.launchers[n] for n in names if self.launchers[n].is_watchdog]
        if not launchers:
            return
        lines = self.actions.processes()
        owner = {pid: l.name for l in launchers for pid in watchdog_pids(lines, l.script_path)}
        survivors = stop_watchdogs(
            [l.script_path for l in launchers], self.actions, self.flow.sleep,
            locks=[self.flow.log_dir / f"{l.name}.lock" for l in launchers])
        if survivors:
            names_left = dict.fromkeys(owner.get(pid, f"pid {pid}") for pid in survivors)
            raise _Failed(f"watchdog did not stop: {', '.join(names_left)}")

    def _quit(self, pids: list[int], labels: dict[int, str]) -> list[int]:
        """Quit those of `pids` a fresh `ps` still shows as Claude mains, never the target's.

        Returns the pids it quit.
        """
        lines = self.actions.processes()
        mains: dict[int, MainProcess] = {}
        for app in dict.fromkeys(l.claude_app for l in self.launchers.values()):
            for proc in main_processes(lines, claude_binary(app)):
                mains[proc.pid] = proc
        target = self.launchers[self.target]
        matched, _ = match_instances(list(mains.values()), [target], self.flow.default_data_dir)
        target_pids = {r.pid for r in matched[self.target]}
        live = [pid for pid in pids if pid in mains and pid not in target_pids]
        if not live:
            return []
        self.asked_to_quit.update(live)
        try:
            survivors = quit_pids(live, self.actions, self.flow.sleep)
        except QuitRequestFailed as e:
            raise _Failed(f"could not ask to quit: {e}") from None
        if survivors:
            self.refused_to_quit.update(survivors)
            left = dict.fromkeys(labels.get(pid, f"pid {pid}") for pid in survivors)
            raise _Failed(f"did not quit: {', '.join(left)}")
        return live

    # -- the sign-in itself, spec steps 3 to 6 --

    def sign_in(self) -> str | None:
        """Return the target's account once the login is seen; raise to stop early."""
        plan = self.plan
        self._phase(SigninPhase.QUITTING, f"quitting everything but {self.target}")
        self._stop_watchdogs(list(dict.fromkeys(plan.quit_rows + plan.watchdog_rows)))
        self._quit(plan.quit_pids, _pid_labels(plan.snapshot))
        self._check_cancel()

        snapshot = self._launch_target()
        snapshot = self._isolate(snapshot)
        row = snapshot.row(self.target)
        account0, created0 = row.account, row.session_created

        self._phase(SigninPhase.WAITING_FOR_LOGIN, f"Sign in now in {self.target}. Waiting...")
        warned: set[int] = set()
        while True:
            self._check_cancel()
            self.flow.sleep(POLL_S)
            self._check_cancel()
            snapshot = self._refresh()
            self._check_cancel()
            row = snapshot.row(self.target)
            if row.is_duplicate:
                raise _Failed(f"{self.target} has more than one process on its folder")
            if not row.is_running:
                raise _Failed(f"{self.target} stopped while waiting for the login")
            for pid in self._other_pids(snapshot):
                if pid not in warned:
                    warned.add(pid)
                    warning = f"another Claude started (pid {pid}); the login may land there"
                    self.warnings.append(warning)
                    self._phase(SigninPhase.WAITING_FOR_LOGIN, warning)
            created = row.session_created
            if row.account is not None and row.account != account0:
                return row.account
            if created is not None and (created0 is None or created > created0):
                return row.account

    def _launch_target(self) -> Snapshot:
        name = self.target
        self._phase(SigninPhase.LAUNCHING, f"starting {name}")
        # A fresh ps decides, not the plan: the target may have started or stopped since.
        if not self._still_running(self.launchers[name]):
            result = self.flow.launch_one(self.launchers[name])
            # A launch requested moments ago is on its way: wait for it, never reopen.
            if (not result.ok and result.message != "already running"
                    and not result.message.endswith(LAUNCH_PENDING)):
                raise _Failed(f"{name} was not launched: {result.message}")
        waited = 0
        while True:
            self._check_cancel()
            snapshot = self._refresh()
            row = snapshot.row(name)
            if row.is_duplicate:
                raise _Failed(f"{name} has more than one process on its folder")
            if row.is_running:
                return snapshot
            if waited >= LAUNCH_TIMEOUT_S:
                raise _Failed(f"{name} did not start")
            self.flow.sleep(POLL_S)
            waited += POLL_S

    def _other_pids(self, snapshot: Snapshot) -> list[int]:
        pids = [pid for r in snapshot.rows if r.name != self.target for pid in r.pids]
        return pids + [p.pid for p in snapshot.strays if p.pid not in pids]

    def _isolate(self, snapshot: Snapshot) -> Snapshot:
        """Quit anything but the target until a snapshot shows the target running alone."""
        self._phase(SigninPhase.ISOLATING, f"checking that {self.target} is the only Claude")
        for attempt in range(ISOLATE_ROUNDS + 1):
            self._check_cancel()
            row = snapshot.row(self.target)
            if row.is_duplicate:
                raise _Failed(f"{self.target} has more than one process on its folder")
            if not row.is_running:
                raise _Failed(f"{self.target} stopped before the login")
            others = [r for r in snapshot.rows if r.name != self.target and r.pids]
            pids = self._other_pids(snapshot)
            if not pids:
                return snapshot
            if attempt == ISOLATE_ROUNDS:
                break
            added = [r.name for r in others
                     if _restorable(r) and r.name not in self.restore_list]
            if added:
                self.restore_list = sorted(self.restore_list + added,
                                           key=lambda n: self.positions.get(n, len(self.positions)))
                write_restore(self.flow.restore_path, self.restore_list)
            self._stop_watchdogs([r.name for r in others])
            labels = _pid_labels(snapshot)
            for pid in self._quit(pids, labels):
                label = labels[pid].removesuffix(f" (pid {pid})")
                self.warnings.append(f"quit {label} (pid {pid}) that started during sign-in")
            snapshot = self._refresh()
        raise _Failed("other Claude processes kept starting; sign-in stopped")

    # -- spec step 8 --

    def _still_running(self, launcher: Launcher) -> bool:
        try:
            lines = self.actions.processes()
        except Exception:
            return False  # guarded_launch re-reads ps and refuses if it still fails
        procs = main_processes(lines, claude_binary(launcher.claude_app))
        matched, _ = match_instances(procs, [launcher], self.flow.default_data_dir)
        return bool(matched[launcher.name])

    def _settle(self) -> None:
        """Wait up to `RESTORE_SETTLE_S` for processes asked to quit to exit.

        Pids that already outlasted the quit timeout are not waited on again.
        """
        pending = self.asked_to_quit - self.refused_to_quit
        waited = 0
        while pending:
            try:
                pending = {pid for pid in pending if self.actions.is_alive(pid)}
            except Exception:
                return
            if not pending or waited >= RESTORE_SETTLE_S:
                return
            self.flow.sleep(SETTLE_POLL_S)
            waited += SETTLE_POLL_S

    def restore(self) -> tuple[list[str], list[tuple[str, str]]]:
        """Relaunch the restore list in launch order; keep refusals in the restore file."""
        restored: list[str] = []
        failed: list[tuple[str, str]] = []
        if self.restore_list:
            self._phase(SigninPhase.RESTORING, f"relaunching {', '.join(self.restore_list)}")
            self._settle()
        gap_due = False
        for name in self.restore_list:
            launcher = self.launchers[name]
            if gap_due:
                self.flow.sleep(LAUNCH_GAP_S)
            try:
                rearm = self._still_running(launcher)
                result = self.flow.restore_one(launcher, rearm)
            except Exception as e:
                failed.append((name, f"relaunch failed ({type(e).__name__})"))
                gap_due = False
                continue
            if result.ok:
                restored.append(name)
            else:
                failed.append((name, result.message))
            gap_due = result.ok
        try:
            if failed:
                write_restore(self.flow.restore_path, [name for name, _ in failed])
            else:
                clear_restore(self.flow.restore_path)
        except OSError as e:
            self.warnings.append(
                f"could not update the restore file ({e.strerror or type(e).__name__})")
        return restored, failed

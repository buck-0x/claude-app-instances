"""The instances TUI: one table of Claude instances, their accounts and usage, and actions.

Threading rules. The slow `Backend` calls (quitting, launching, launch all, re-arm,
bring to front, live lookups) run in thread workers. Quick local reads stay on the
event loop: `refresh_state`, `do_signin` and `do_move` call `snapshot()` (one `ps`
read plus small files) there. Every worker body and every loop-side callback catches
its own exceptions into a one-line status naming only the exception type, because
Textual prints an uncaught one as a traceback with local variables and those could
hold account details. Results reach the loop through
`call_from_thread` (or, from the sign-in thread, through `post_message`, which never
blocks, so `finish_signin` may join that thread from the loop). The `LiveScheduler`
is touched on the event loop only.

Exiting while a sign-in runs cancels it and waits for its restore first; no key or
built-in action exits without that.
"""

import asyncio
import functools
import os
import shlex
import signal
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from claude_instances.accounts import short_id
from claude_instances.actions import AUTOMATION_HELP, QuitOutcome
from claude_instances.backend import Backend
from claude_instances.cookies import SessionState
from claude_instances.paths import RESTORE_FILE
from claude_instances.scheduler import STATE_INTERVAL_S, LiveScheduler
from claude_instances.screens import (
    ConfirmScreen,
    DetailsScreen,
    RestoreOfferScreen,
    SigninScreen,
    details_text,
)
from claude_instances.signin import (
    SigninPhase,
    SigninPlan,
    SigninRefused,
    SigninResult,
    clear_restore,
    plan_signin,
    read_restore,
    write_restore,
)
from claude_instances.snapshot import InstanceState, Snapshot
from claude_instances.usage import NO_DATA, Usage, format_cell, is_live, level

LIVE_TICK_S = 1
ACTION_GROUP = "action"
BUSY_MESSAGE = "an action is still running; sign in once it finishes"

LIVE_LABELS = {
    "none": "cached only (started without --key-fd)",
    "unverified": "live usage: checking key",
    "ok": "live usage ✓",
    "bad": "live usage: key cannot decrypt cookies; showing cached",
}

LEVEL_STYLES = {"warn": "bold yellow", "crit": "bold red"}

# launch_all's messages for rows it did not try to launch: skips, not failures.
_LAUNCH_ALL_SKIPS = ("already running", "duplicates running; skipped")

# Screen actions refused while a sign-in runs.
_BLOCKED_DURING_SIGNIN = frozenset(
    {"front", "launch", "quit_instance", "rearm", "launch_all", "signin", "mark_expected",
     "move_up", "move_down"})

COLUMNS = ("#", "Instance", "State", "Account", "5h", "Week")


def guarded(label: str) -> Callable:
    """Make an event-loop method turn any exception into the status `<label> failed (X)`.

    An uncaught exception would end the app with a traceback that prints locals.
    """

    def wrap(method: Callable) -> Callable:
        @functools.wraps(method)
        def inner(self: "InstancesApp", *args: object, **kwargs: object) -> object:
            try:
                return method(self, *args, **kwargs)
            except Exception as e:
                try:
                    self.status_text = f"{label} failed ({type(e).__name__})"
                except Exception:
                    pass
                return None
        return inner
    return wrap


# ---- cell text ------------------------------------------------------------------------

def state_cell(row: InstanceState) -> Text:
    if row.is_duplicate:
        return Text("DUPLICATE", style="bold red")
    if row.is_running:
        if row.argless:
            return Text("● open (not started by a launcher)", style="green")
        return Text("● open", style="green")
    return Text("○ closed", style="dim")


def account_cell(row: InstanceState) -> Text:
    who = row.email or (short_id(row.account) if row.account else None)
    if row.session is SessionState.REJECTED:
        base = "session rejected"
    elif row.session is SessionState.SIGNED_IN:
        base = who or "signed in"
    elif row.session is SessionState.SIGNED_OUT and row.account:
        base = f"last: {short_id(row.account)}"
    elif row.session is SessionState.SIGNED_OUT:
        base = "signed out"
    else:
        base = who or NO_DATA
    text = Text()
    if row.shared_with:
        text.append(f"⚠ {base} (shared with {', '.join(row.shared_with)})", style="bold red")
    elif row.expected_mismatch and row.expected:
        text.append(f"⚠ {base} (expected {short_id(row.expected)})", style="bold yellow")
    else:
        text.append(base)
    if row.live_error:
        text.append(f"  {row.live_error}", style="dim")
    return text


def usage_cell(pct: float | None, resets_at: datetime | None, usage: Usage | None,
               now: datetime) -> Text:
    text = format_cell(pct, resets_at, usage, now)
    style = LEVEL_STYLES.get(level(pct), "")
    if usage is not None and pct is not None and not is_live(usage, now):
        style = f"{style} dim".strip()
    return Text(text, style=style)


def row_cells(row: InstanceState, now: datetime) -> tuple:
    u = row.usage
    return (
        str(row.position + 1),
        row.name,
        state_cell(row),
        account_cell(row),
        usage_cell(u.five_hour_pct if u else None, u.five_hour_resets_at if u else None, u, now),
        usage_cell(u.week_pct if u else None, u.week_resets_at if u else None, u, now),
    )


def launch_all_message(results: list) -> str:
    launched = [name for name, r in results if r.ok]
    skipped = [name for name, r in results if not r.ok and r.message in _LAUNCH_ALL_SKIPS]
    failed = [f"{name} ({r.message})" for name, r in results
              if not r.ok and r.message not in _LAUNCH_ALL_SKIPS]
    parts = []
    if launched:
        parts.append(f"launched {', '.join(launched)}")
    if skipped:
        parts.append(f"skipped {', '.join(skipped)} (running)")
    if failed:
        parts.append(f"not launched: {', '.join(failed)}")
    return "launch all: " + ("; ".join(parts) if parts else "no instances")


def signin_message(target: str, result: SigninResult) -> str:
    message = f"{target}: {result.message}"
    if result.restore_failed:
        failed = ", ".join(f"{name} ({why})" for name, why in result.restore_failed)
        message += f"; not relaunched: {failed}"
    if result.warnings:
        message += f"; {'; '.join(result.warnings)}"
    return message


def carry_restore(plan: SigninPlan, pending: list[str]) -> tuple[list[str], list[str]]:
    """Add `pending` names left by an earlier sign-in to `plan.restore`, in launch order.

    Returns (carried, dropped): the names added, and the names that cannot be
    relaunched (no launcher, a duplicate or an old one-shot launcher). The target is
    never added.
    """
    rows = {r.name: r for r in plan.snapshot.rows}
    carried: list[str] = []
    dropped: list[str] = []
    for name in dict.fromkeys(pending):
        if name == plan.target or name in plan.restore:
            continue
        row = rows.get(name)
        if row is None or row.is_duplicate or not row.launcher.is_watchdog:
            dropped.append(name)
        else:
            carried.append(name)
    if carried:
        plan.restore = sorted(plan.restore + carried, key=lambda n: rows[n].position)
    return carried, dropped


# ---- messages from the sign-in thread -------------------------------------------------

class SigninProgress(Message):
    def __init__(self, phase: SigninPhase, text: str) -> None:
        super().__init__()
        self.phase = phase
        self.text = text


class SigninFinished(Message):
    def __init__(self, target: str, result: SigninResult) -> None:
        super().__init__()
        self.target = target
        self.result = result


# ---- the main screen ------------------------------------------------------------------

class InstancesScreen(Screen):
    """The table and every per-row action; a modal on top blocks all of these."""

    DEFAULT_CSS = """
    #header { background: $panel; padding: 0 1; }
    #status { padding: 0 1; height: auto; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        Binding("f", "front", "front"),
        Binding("l", "launch", "launch"),
        Binding("q", "quit_instance", "quit"),
        Binding("w", "rearm", "re-arm"),
        Binding("a", "launch_all", "launch all"),
        Binding("r", "refresh_usage", "refresh"),
        Binding("s", "signin", "sign in"),
        Binding("d", "details", "details"),
        Binding("o", "open_log", "log"),
        Binding("m", "mark_expected", "mark expected"),
        Binding("shift+up", "move_up", "move up"),
        Binding("shift+down", "move_down", "move down"),
    ]

    @property
    def instances_app(self) -> "InstancesApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield Static(Text(""), id="header")
        table: DataTable = DataTable(cursor_type="row", zebra_stripes=True)
        for column in COLUMNS:
            table.add_column(column, key=column)
        yield table
        yield Static(Text(""), id="status")
        yield Footer()

    # Set once the widgets exist; `is_mounted` turns true only after on_mount returns.
    ready = False

    def on_mount(self) -> None:
        self.ready = True
        # Status set before the screen mounted (say, a restore file error) shows now.
        self.show_status(self.instances_app.status_text)
        self.instances_app.refresh_state()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in _BLOCKED_DURING_SIGNIN and self.instances_app.signin_active:
            return False
        return True

    def show_header(self, text: str) -> None:
        self.query_one("#header", Static).update(Text(text))

    def show_status(self, text: str) -> None:
        self.query_one("#status", Static).update(Text(text))

    def show_snapshot(self, snapshot: Snapshot) -> None:
        table = self.query_one(DataTable)
        current = self.current_name()
        table.clear()
        for row in snapshot.rows:
            table.add_row(*row_cells(row, snapshot.taken_at), key=row.name)
        if current is not None:
            self.select_name(current)

    def current_name(self) -> str | None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return None
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        return key.value

    def select_name(self, name: str) -> None:
        table = self.query_one(DataTable)
        try:
            table.move_cursor(row=table.get_row_index(name))
        except Exception:
            pass

    # Each action delegates to the app, which owns the backend and the workers.
    def action_front(self) -> None:
        self.instances_app.do_front()

    def action_launch(self) -> None:
        self.instances_app.do_launch()

    def action_quit_instance(self) -> None:
        self.instances_app.do_quit()

    def action_rearm(self) -> None:
        self.instances_app.do_rearm()

    def action_launch_all(self) -> None:
        self.instances_app.do_launch_all()

    def action_refresh_usage(self) -> None:
        self.instances_app.do_refresh_usage()

    def action_signin(self) -> None:
        self.instances_app.do_signin()

    def action_details(self) -> None:
        self.instances_app.do_details()

    def action_open_log(self) -> None:
        self.instances_app.do_open_log()

    def action_mark_expected(self) -> None:
        self.instances_app.do_mark_expected()

    def action_move_up(self) -> None:
        self.instances_app.do_move(-1)

    def action_move_down(self) -> None:
        self.instances_app.do_move(1)


# ---- the app --------------------------------------------------------------------------

class InstancesApp(App):
    TITLE = "Claude instances"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "request_exit", "exit", priority=True),
        Binding("ctrl+c", "request_exit", "exit", show=False, priority=True),
    ]

    def __init__(self, backend: Backend) -> None:
        super().__init__()
        self.backend = backend
        self.scheduler = LiveScheduler()
        self.main_screen = InstancesScreen()
        self.snapshot: Snapshot | None = None
        self._header_text = ""
        self._status_text = ""
        self._refreshed_at: datetime | None = None
        self._shown_expected_error: str | None = None
        self.signin_thread: threading.Thread | None = None
        self.signin_cancel: threading.Event | None = None
        self.signin_active = False
        self.exit_after_signin = False
        self._signin_screen: SigninScreen | None = None
        self._signals: list[int] = []

    # -- header and status --

    @property
    def header_text(self) -> str:
        return self._header_text

    @property
    def status_text(self) -> str:
        return self._status_text

    @status_text.setter
    def status_text(self, text: str) -> None:
        self._status_text = text
        if self.main_screen.ready:
            self.main_screen.show_status(text)

    def _update_header(self) -> None:
        live = LIVE_LABELS[self.backend.key_status]
        refreshed = (self._refreshed_at.astimezone().strftime("%H:%M:%S")
                     if self._refreshed_at is not None else NO_DATA)
        self._header_text = f"Claude instances    {live}    refreshed {refreshed}"
        if self.main_screen.ready:
            self.main_screen.show_header(self._header_text)

    # -- lifecycle --

    def get_default_screen(self) -> Screen:
        return self.main_screen

    def on_mount(self) -> None:
        # The first refresh_state runs when the main screen mounts.
        self.set_interval(STATE_INTERVAL_S, self.refresh_state)
        self.set_interval(LIVE_TICK_S, self.live_tick)
        self._add_signal_handlers()
        self._offer_restore()

    def on_unmount(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for sig in self._signals:
            loop.remove_signal_handler(sig)
        self._signals = []

    def _add_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                loop.add_signal_handler(sig, self.action_request_exit)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            self._signals.append(sig)

    def _from_thread(self, callback: Callable, *args: object) -> None:
        """Run `callback` on the loop from a worker thread; a no-op once the app has exited."""
        try:
            self.call_from_thread(callback, *args)
        except Exception:
            pass

    # -- state refresh --

    def refresh_state(self) -> bool:
        """Take a fresh snapshot and show it; on failure keep the table and say so."""
        try:
            snapshot = self.backend.snapshot()
        except Exception as e:
            self.status_text = f"refresh failed ({type(e).__name__})"
            return False
        try:
            self.snapshot = snapshot
            self._refreshed_at = snapshot.taken_at
            if not self.backend.cached_only:
                self.scheduler.set_candidates(
                    [r.name for r in snapshot.rows
                     if r.session in (SessionState.SIGNED_IN, SessionState.REJECTED)])
            error = self.backend.expected_error
            if error is not None and error != self._shown_expected_error:
                self.status_text = error
            self._shown_expected_error = error
            self._update_header()
            if self.main_screen.ready:
                self.main_screen.show_snapshot(snapshot)
        except Exception as e:
            self.status_text = f"display failed ({type(e).__name__})"
            return False
        return True

    # -- live lookups --

    @guarded("live lookup")
    def live_tick(self) -> None:
        """Start the next due live lookup, if any; runs on the event loop."""
        if self.backend.cached_only:
            return
        name = self.scheduler.next_due()
        if name is None:
            return
        self.scheduler.start(name)
        backend = self.backend

        def body() -> None:
            ok = False
            try:
                ok = backend.live_lookup(name).usage is not None
            except Exception:
                ok = False
            finally:
                self._from_thread(self._live_finished, name, ok)

        try:
            self.run_worker(body, thread=True, group="live", exit_on_error=False)
        except Exception:
            # Never leave the scheduler in flight, or live lookups stop for the session.
            self.scheduler.finish(name, False)
            raise

    @guarded("live lookup")
    def _live_finished(self, name: str, ok: bool) -> None:
        self.scheduler.finish(name, ok)
        self.refresh_state()

    # -- helpers for actions --

    def _current_row(self) -> InstanceState | None:
        name = self.main_screen.current_name()
        if name is None or self.snapshot is None:
            return None
        try:
            return self.snapshot.row(name)
        except KeyError:
            return None

    def _run_action(self, label: str, body: Callable[[], str]) -> None:
        """Run `body` in a thread worker; its message, or the failure, becomes the status."""

        def work() -> None:
            try:
                message = body()
            except Exception as e:
                message = f"{label} failed ({type(e).__name__})"
            self._from_thread(self._action_done, message)

        self.run_worker(work, thread=True, group=ACTION_GROUP, exit_on_error=False)

    def actions_running(self) -> bool:
        """True while any launch, quit, re-arm, front or relaunch worker is unfinished."""
        return any(w.group == ACTION_GROUP and not w.is_finished for w in self.workers)

    @guarded("refresh")
    def _action_done(self, message: str) -> None:
        self.status_text = message
        if not self.refresh_state():
            self.status_text = f"{message}; {self.status_text}"

    # -- actions --

    @guarded("front")
    def do_front(self) -> None:
        row = self._current_row()
        if row is None:
            return
        name = row.name
        if row.is_duplicate:
            self.status_text = f"{name} has more than one process running; quit one first"
            return
        if not row.is_running:
            self.status_text = f"{name} is not running; press l to launch"
            return

        def body() -> str:
            result = self.backend.front(name)
            if result.ok:
                return f"{name}: brought to front"
            if result.message == AUTOMATION_HELP:
                return AUTOMATION_HELP
            return result.message if name in result.message else f"{name}: {result.message}"

        self._run_action("front", body)

    @guarded("launch")
    def do_launch(self) -> None:
        row = self._current_row()
        if row is None:
            return
        name = row.name
        self._run_action("launch", lambda: f"{name}: {self.backend.launch(name).message}")

    @guarded("re-arm")
    def do_rearm(self) -> None:
        row = self._current_row()
        if row is None:
            return
        name = row.name

        def body() -> str:
            result = self.backend.rearm(name)
            return f"{name}: watchdog re-armed" if result.ok else f"{name}: {result.message}"

        self._run_action("re-arm", body)

    @guarded("launch all")
    def do_launch_all(self) -> None:
        self._run_action("launch all", lambda: launch_all_message(self.backend.launch_all()))

    @guarded("quit")
    def do_quit(self) -> None:
        row = self._current_row()
        if row is None:
            return
        name = row.name

        def body() -> str:
            outcome, message = self.backend.quit(name)
            if outcome is QuitOutcome.DID_NOT_QUIT:
                return f"{name}: {message}; press w to re-arm the watchdog"
            return f"{name}: {message}"

        def answered(yes: bool | None) -> None:
            if yes:
                self.status_text = f"quitting {name}..."
                self._run_action("quit", body)

        self.push_screen(ConfirmScreen(f"Quit {name}? Its watchdog is stopped first."),
                         answered)

    @guarded("refresh usage")
    def do_refresh_usage(self) -> None:
        row = self._current_row()
        if row is None:
            return
        if self.backend.cached_only:
            self.status_text = "live usage is off (started without --key-fd)"
            return
        if row.session not in (SessionState.SIGNED_IN, SessionState.REJECTED):
            self.status_text = f"{row.name} has no session to look up"
            return
        self.scheduler.request_now(row.name)
        self.status_text = f"{row.name}: live lookup queued"
        self.live_tick()

    @guarded("details")
    def do_details(self) -> None:
        row = self._current_row()
        if row is None:
            return
        self.push_screen(DetailsScreen(
            details_text(row, self.backend.support_dir, self.backend.log_dir)))

    @guarded("open log")
    def do_open_log(self) -> None:
        row = self._current_row()
        if row is None:
            return
        log = self.backend.log_dir / f"{row.name}.log"
        if not log.is_file():
            self.status_text = f"no watchdog log for {row.name}"
            return
        try:
            with self.suspend():
                subprocess.run(shlex.split(os.environ.get("PAGER") or "less") + [str(log)])
        except Exception as e:
            self.status_text = f"could not open the log ({type(e).__name__})"

    @guarded("mark expected")
    def do_mark_expected(self) -> None:
        row = self._current_row()
        if row is None:
            return
        if row.account is None:
            self.status_text = f"{row.name} has no known account"
            return
        who = row.email or short_id(row.account)
        self._confirm_expected(row.name, who)

    @guarded("mark expected")
    def _confirm_expected(self, name: str, who: str) -> None:
        def answered(yes: bool | None) -> None:
            if yes:
                try:
                    self.status_text = self.backend.mark_expected(name).message
                except Exception as e:
                    self.status_text = f"mark expected failed ({type(e).__name__})"
                self.refresh_state()

        self.push_screen(
            ConfirmScreen(f"Record {who} as the expected account for {name}?"), answered)

    @guarded("move")
    def do_move(self, delta: int) -> None:
        name = self.main_screen.current_name()
        if name is None:
            return
        try:
            self.backend.move(name, delta)
        except Exception as e:
            self.status_text = f"move failed ({type(e).__name__})"
            return
        self.refresh_state()
        self.main_screen.select_name(name)

    # -- restore offer --

    @guarded("restore check")
    def _offer_restore(self) -> None:
        try:
            names = self.backend.pending_restore()
        except (OSError, UnicodeDecodeError) as e:
            self.status_text = f"could not read the restore file ({type(e).__name__})"
            return
        except Exception as e:
            self.status_text = f"restore check failed ({type(e).__name__})"
            return
        if names:
            self.push_screen(RestoreOfferScreen(names), lambda yes: self._restore(names, yes))

    @guarded("relaunch")
    def _restore(self, names: list[str], yes: bool | None) -> None:
        if not yes:
            self.status_text = "restore list kept; offered again next start"
            return
        backend = self.backend
        path = backend.config_dir / RESTORE_FILE

        def body() -> str:
            # Running rows come back as skips: nothing is left to relaunch for them.
            results = backend.launch_all(names)
            seen = {name for name, _ in results}
            launched = [name for name, r in results if r.ok]
            running = [name for name, r in results
                       if not r.ok and r.message in _LAUNCH_ALL_SKIPS]
            failed = [(name, r.message) for name, r in results
                      if not r.ok and r.message not in _LAUNCH_ALL_SKIPS]
            gone = [name for name in names if name not in seen]
            parts = []
            if launched:
                parts.append(f"relaunched {', '.join(launched)}")
            if running:
                parts.append(f"already running: {', '.join(running)}")
            if gone:
                parts.append(f"no launcher, dropped: {', '.join(gone)}")
            if failed:
                reasons = ", ".join(f"{name} ({why})" for name, why in failed)
                parts.append(f"not launched: {reasons}; kept for next start")
            try:
                if failed:
                    write_restore(path, [name for name, _ in failed])
                else:
                    clear_restore(path)
            except OSError as e:
                parts.append(f"could not update the restore file ({type(e).__name__})")
            return "relaunch: " + ("; ".join(parts) if parts else "nothing to do")

        self._run_action("relaunch", body)

    # -- sign-in --

    @guarded("sign in")
    def do_signin(self) -> None:
        row = self._current_row()
        if row is None or self.signin_active:
            return
        name = row.name
        if self.actions_running():
            self.status_text = BUSY_MESSAGE
            return
        try:
            plan = plan_signin(self.backend.snapshot(), name)
        except SigninRefused as e:
            self.status_text = f"{name}: {e}"
            return
        except Exception as e:
            self.status_text = f"sign in failed ({type(e).__name__})"
            return
        try:
            owner, pending = read_restore(self.backend.config_dir / RESTORE_FILE)
        except (OSError, UnicodeDecodeError) as e:
            self.status_text = (
                f"could not read the restore file ({type(e).__name__}); not signing in")
            return
        if pending and owner is not None and owner != os.getpid():
            try:
                owner_running = self.backend.restore_owner_running(owner)
            except Exception as e:
                self.status_text = (f"could not check the restore file's owner"
                                    f" ({type(e).__name__}); not signing in")
                return
            if owner_running:
                path = self.backend.config_dir / RESTORE_FILE
                self.status_text = (
                    f"another sign-in (pid {owner}) is running; try again after it"
                    f" (if none is, delete {path})")
                return
        # The flow rewrites the restore file, so a list left by an earlier sign-in is
        # carried into this one instead of being lost.
        carried, dropped = carry_restore(plan, pending)
        lines = [f"Sign in {name}?"]
        if plan.quit_rows:
            lines.append(f"Will be quit: {', '.join(plan.quit_rows)}")
        if plan.watchdog_rows:
            lines.append(f"Watchdogs stopped: {', '.join(plan.watchdog_rows)}")
        if plan.not_restorable:
            lines.append("Will be quit and not relaunched: " + ", ".join(plan.not_restorable))
        if plan.restore:
            lines.append(f"Relaunched afterwards: {', '.join(plan.restore)}")
        if carried:
            lines.append("(includes, left by an earlier sign-in: " + ", ".join(carried) + ")")
        if dropped:
            lines.append("Dropped from the earlier relaunch list: " + ", ".join(dropped))
        if len(lines) == 1:
            lines.append("Nothing else is running.")

        def answered(yes: bool | None) -> None:
            if yes:
                self._start_signin(plan)

        self.push_screen(ConfirmScreen("\n".join(lines)), answered)

    @guarded("sign in")
    def _start_signin(self, plan: SigninPlan) -> None:
        if self.signin_active:
            return
        if self.actions_running():
            self.status_text = BUSY_MESSAGE
            return
        cancel = threading.Event()
        self.signin_cancel = cancel
        self.signin_active = True
        self._signin_screen = SigninScreen(plan.target, cancel.set)
        self.push_screen(self._signin_screen)
        backend = self.backend

        def post(message: Message) -> None:
            try:
                self.post_message(message)
            except Exception:
                pass

        def on_phase(phase: SigninPhase, text: str) -> None:
            post(SigninProgress(phase, text))

        def body() -> None:
            result = SigninResult(False, "sign-in failed")
            try:
                flow = backend.make_signin_flow(on_phase)
                result = flow.run(plan, cancel)
            except Exception as e:
                result = SigninResult(False, f"sign-in failed ({type(e).__name__})")
            finally:
                post(SigninFinished(plan.target, result))

        self.signin_thread = threading.Thread(target=body, name="signin", daemon=False)
        self.signin_thread.start()

    @guarded("sign-in display")
    def on_signin_progress(self, message: SigninProgress) -> None:
        screen = self._signin_screen
        if screen is None:
            return
        if message.text.startswith("another Claude started"):
            screen.add_warning(message.text)
        else:
            screen.show_phase(message.text)

    def on_signin_finished(self, message: SigninFinished) -> None:
        self.signin_active = False
        try:
            self._close_signin_screen()
        except Exception:
            pass
        if self.exit_after_signin:
            self.exit()
            return
        self._after_signin(message.target, message.result)

    def _close_signin_screen(self) -> None:
        """Remove the sign-in screen even when another screen was pushed above it."""
        screen, self._signin_screen = self._signin_screen, None
        if screen is None or screen not in self.screen_stack:
            return
        while self.screen_stack[-1] is not screen:
            self.pop_screen()
        self.pop_screen()

    @guarded("sign-in result")
    def _after_signin(self, target: str, result: SigninResult) -> None:
        self.status_text = signin_message(target, result)
        self.refresh_state()
        if result.ok and result.new_account is not None:
            who = short_id(result.new_account)
            if self.snapshot is not None:
                try:
                    row = self.snapshot.row(target)
                    if row.account == result.new_account and row.email:
                        who = row.email
                except KeyError:
                    pass
            self._confirm_expected(target, who)

    def finish_signin(self, timeout: float | None = None) -> None:
        """Cancel a running sign-in and wait for its restore; a no-op when none ran."""
        if self.signin_cancel is not None:
            self.signin_cancel.set()
        if self.signin_thread is not None:
            self.signin_thread.join(timeout)

    # -- exit --

    def action_request_exit(self) -> None:
        if not self.signin_active:
            self.exit()
            return
        if self.signin_cancel is not None:
            self.signin_cancel.set()
        self.exit_after_signin = True
        self.status_text = "Restoring instances before exit..."
        if self._signin_screen is not None:
            self._signin_screen.show_phase("Restoring instances before exit...")

    def action_quit(self) -> None:  # type: ignore[override]
        self.action_request_exit()

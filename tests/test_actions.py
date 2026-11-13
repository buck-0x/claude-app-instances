import signal
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from helpers import FakeSystemActions

from claude_instances.actions import (
    LAUNCH_GAP_S,
    QUIT_SIGNAL,
    ActionResult,
    MacSystemActions,
    QuitOutcome,
    automation_denied,
    guarded_launch,
    launch_all,
    quit_instance,
    remove_lock_if_names,
)
from claude_instances.cookies import SessionState
from claude_instances.launchers import Launcher
from claude_instances.processes import ProcessLine, Running
from claude_instances.snapshot import InstanceState, Snapshot

WD = "/Apps/Claude C.app/Contents/MacOS/launcher"
CLAUDE_APP = Path("/Applications/FakeClaude.app")
BIN = f"{CLAUDE_APP}/Contents/MacOS/Claude"
DEFAULT = Path("/Support/Claude")
LOGS = Path("/Logs")  # only the fake sees it; nothing touches the filesystem
LOCK = LOGS / "Claude C.lock"
C_DIR = Path("/Support/Claude C")
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def launcher(
    name: str = "Claude C",
    fmt: str = "argless",
    data_dir: Path | None = None,
    script: str | None = None,
) -> Launcher:
    app = Path(f"/Apps/{name}.app")
    return Launcher(
        name=name,
        app_path=app,
        script_path=Path(script) if script else app / "Contents/MacOS/launcher",
        data_dir=data_dir or Path(f"/Support/{name}"),
        claude_app=CLAUDE_APP,
        is_watchdog=fmt != "oneshot",
        has_argless_rule=fmt == "argless",
    )


def state(name: str, pids: list[int], script: str | None = None,
          data_dir: Path | None = None) -> InstanceState:
    return InstanceState(
        name=name,
        launcher=launcher(name, script=script, data_dir=data_dir),
        position=0,
        running=[Running(pid, argless=False) for pid in pids],
        session=SessionState.SIGNED_IN,
        session_expiry=None,
        session_created=None,
        account=None,
        email=None,
        org=None,
        expected=None,
        shared_with=[],
        expected_mismatch=False,
        usage=None,
        live_error=None,
        watchdog_alive=False,
    )


def state_running(pids: list[int], script: str | None = None) -> InstanceState:
    return state("Claude C", pids, script=script)


def closed(name: str) -> InstanceState:
    return state(name, [])


def running(name: str) -> InstanceState:
    return state(name, [600])


def duplicate(name: str) -> InstanceState:
    return state(name, [601, 602])


def snapshot(rows: list[InstanceState]) -> Snapshot:
    return Snapshot(rows=rows, strays=[], taken_at=NOW)


def no_sleep(_s: float) -> None:
    pass


def ps_fails(method: str) -> None:
    if method == "processes":
        raise subprocess.CalledProcessError(1, ["ps"])


def main_line(pid: int, data_dir: Path = C_DIR) -> ProcessLine:
    return ProcessLine(pid, f"{BIN} --user-data-dir={data_dir}")


def wd_line(pid: int = 508) -> ProcessLine:
    return ProcessLine(pid, f"/bin/zsh {WD}")


def quit_c(fake: FakeSystemActions, pids: list[int], script: str | None = WD, sleep=no_sleep):
    return quit_instance(state_running(pids, script=script), fake, sleep=sleep,
                         default_data_dir=DEFAULT, log_dir=LOGS)


def calls_named(fake: FakeSystemActions, name: str) -> list[tuple]:
    return [c for c in fake.calls if c[0] == name]


# ---- quit ---------------------------------------------------------------------

def test_quit_stops_and_waits_for_watchdog_then_quits():
    fake = FakeSystemActions(alive={501, 508}, ps=[wd_line(), main_line(501)])
    outcome, _ = quit_c(fake, [501])
    signals = [c for c in fake.calls if c[0] in ("signal_pids", "send_quit")]
    assert signals[:2] == [("signal_pids", [508], signal.SIGTERM), ("send_quit", 501)]
    w, q = fake.calls.index(signals[0]), fake.calls.index(signals[1])
    assert ("processes",) in fake.calls[w + 1:q]  # watchdog exit confirmed by a ps poll first
    assert outcome is QuitOutcome.QUIT


def test_stuck_watchdog_escalates_then_aborts():
    fake = FakeSystemActions(alive={501, 508}, ps=[wd_line(), main_line(501)],
                             never_dies={508}, locks={LOCK: 508})
    outcome, message = quit_c(fake, [501])
    assert (outcome, message) == (
        QuitOutcome.WATCHDOG_STUCK, "watchdog did not stop; instance left running")
    assert ("signal_pids", [508], signal.SIGKILL) in fake.calls
    assert ("send_quit", 501) not in fake.calls
    assert fake.locks == {LOCK: 508}  # a watchdog still alive keeps its lock


def test_watchdog_stop_waits_up_to_timeout_before_sigkill():
    slept: list[float] = []
    fake = FakeSystemActions(alive={508}, ps=[wd_line()], never_dies={508})
    quit_c(fake, [501], sleep=slept.append)
    kill_at = fake.calls.index(("signal_pids", [508], signal.SIGKILL))
    assert sum(slept) == 5 + 2 and set(slept) == {0.5}
    assert fake.calls[kill_at - 1] == ("processes",)


def test_sigkilled_watchdog_lock_removed_when_it_names_the_killed_pid():
    fake = FakeSystemActions(alive={501, 508}, ps=[wd_line(), main_line(501)],
                             sigterm_ignored={508}, locks={LOCK: 508})
    assert quit_c(fake, [501])[0] is QuitOutcome.QUIT
    kill_at = fake.calls.index(("signal_pids", [508], signal.SIGKILL))
    assert ("remove_lock_if_names", LOCK, 508) in fake.calls[kill_at:]
    assert fake.locks == {}


def test_sigkilled_watchdog_lock_left_when_it_names_another_pid():
    fake = FakeSystemActions(alive={501, 508}, ps=[wd_line(), main_line(501)],
                             sigterm_ignored={508}, locks={LOCK: 999})
    assert quit_c(fake, [501])[0] is QuitOutcome.QUIT
    assert fake.locks == {LOCK: 999}


def test_sigkilled_watchdog_lock_missing_is_fine():
    fake = FakeSystemActions(alive={501, 508}, ps=[wd_line(), main_line(501)],
                             sigterm_ignored={508})
    assert quit_c(fake, [501])[0] is QuitOutcome.QUIT
    assert fake.locks == {}


def test_sigterm_exit_leaves_lock_to_the_watchdog():
    fake = FakeSystemActions(alive={501, 508}, ps=[wd_line(), main_line(501)],
                             locks={LOCK: 508})
    quit_c(fake, [501])
    assert calls_named(fake, "remove_lock_if_names") == []


def test_remove_lock_if_names_on_disk(tmp_path):
    lock = tmp_path / "X.lock"
    assert not remove_lock_if_names(lock, 508)  # missing
    lock.symlink_to("999")
    assert not remove_lock_if_names(lock, 508) and lock.is_symlink()
    assert remove_lock_if_names(lock, 999) and not lock.is_symlink()


def test_quit_timeout_reports_watchdog_stopped():
    fake = FakeSystemActions(alive={501}, ps=[main_line(501)], never_dies={501})
    assert quit_c(fake, [501], script=None) == (
        QuitOutcome.DID_NOT_QUIT, "did not quit; watchdog stopped")


def test_quit_polls_is_alive_every_second_for_thirty_seconds():
    slept: list[float] = []
    fake = FakeSystemActions(alive={501}, ps=[main_line(501)], never_dies={501})
    quit_c(fake, [501], script=None, sleep=slept.append)
    assert slept == [1] * 30


def test_quit_duplicate_quits_both_pids():
    fake = FakeSystemActions(alive={505, 506}, ps=[main_line(505), main_line(506)])
    outcome, _ = quit_c(fake, [505, 506], script=None)
    assert ("send_quit", 505) in fake.calls and ("send_quit", 506) in fake.calls
    assert outcome is QuitOutcome.QUIT


def test_quit_skips_snapshot_pid_reused_by_unrelated_process():
    fake = FakeSystemActions(alive={501}, ps=[ProcessLine(501, "/usr/bin/vim notes.txt")])
    assert quit_c(fake, [501], script=None) == (
        QuitOutcome.QUIT, "already quit; watchdog stopped")
    assert calls_named(fake, "send_quit") == []


def test_quit_skips_pid_now_running_another_instance():
    fake = FakeSystemActions(alive={501, 502},
                             ps=[main_line(501, Path("/Support/Other")), main_line(502)])
    assert quit_c(fake, [501, 502], script=None)[0] is QuitOutcome.QUIT
    assert calls_named(fake, "send_quit") == [("send_quit", 502)]


def test_quit_argless_instance_on_default_folder():
    st = state("Claude", [504], data_dir=DEFAULT)
    fake = FakeSystemActions(alive={504}, ps=[ProcessLine(504, BIN)])
    outcome, _ = quit_instance(st, fake, sleep=no_sleep, default_data_dir=DEFAULT, log_dir=LOGS)
    assert outcome is QuitOutcome.QUIT and ("send_quit", 504) in fake.calls


def test_quit_surfaces_failed_quit_request_without_polling():
    slept: list[float] = []
    fake = FakeSystemActions(alive={501}, ps=[main_line(501)],
                             quit_results={501: ActionResult(False, "permission denied")})
    outcome, message = quit_c(fake, [501], script=None, sleep=slept.append)
    assert outcome is QuitOutcome.DID_NOT_QUIT
    assert "permission denied" in message and "watchdog stopped" in message
    assert slept == [] and calls_named(fake, "is_alive") == []


def test_quit_not_running_without_watchdog_touches_nothing():
    fake = FakeSystemActions(ps=[ProcessLine(700, "/bin/zsh /Apps/Other.app/x")], alive={700})
    assert quit_c(fake, []) == (QuitOutcome.NOT_RUNNING, "not running")
    assert not [c for c in fake.calls if c[0] in ("signal_pids", "send_quit")]


def test_quit_not_running_stops_watchdog_waiting_to_reopen():
    fake = FakeSystemActions(ps=[wd_line()], alive={508})
    assert quit_c(fake, []) == (
        QuitOutcome.NOT_RUNNING, "stopped watchdog waiting to reopen Claude C")
    assert ("signal_pids", [508], signal.SIGTERM) in fake.calls
    assert calls_named(fake, "send_quit") == []


def test_quit_not_running_with_stuck_watchdog_reports_it():
    fake = FakeSystemActions(ps=[wd_line()], alive={508}, never_dies={508})
    assert quit_c(fake, [])[0] is QuitOutcome.WATCHDOG_STUCK


def test_quit_never_signals_claude_as_a_watchdog():
    fake = FakeSystemActions(alive={501, 508}, ps=[wd_line(), main_line(501)])
    quit_c(fake, [501])
    assert calls_named(fake, "signal_pids") == [("signal_pids", [508], signal.SIGTERM)]


def test_quit_refuses_when_ps_cannot_be_read():
    fake = FakeSystemActions(alive={501, 508}, before_call=ps_fails)
    outcome, message = quit_c(fake, [501])
    assert outcome is QuitOutcome.WATCHDOG_STUCK and "left running" in message
    assert not [c for c in fake.calls if c[0] in ("signal_pids", "send_quit")]


def test_quit_refuses_when_ps_fails_after_watchdog_stop():
    reads: list[str] = []

    def third_read_fails(method: str) -> None:
        if method == "processes":
            reads.append(method)
            if len(reads) >= 3:  # find watchdog, confirm it gone, then the pid re-check
                raise subprocess.CalledProcessError(1, ["ps"])

    fake = FakeSystemActions(alive={501, 508}, ps=[wd_line(), main_line(501)],
                             before_call=third_read_fails)
    outcome, message = quit_c(fake, [501])
    assert outcome is QuitOutcome.DID_NOT_QUIT and "watchdog stopped" in message
    assert calls_named(fake, "send_quit") == []


def test_quit_signal_is_sigterm():
    assert QUIT_SIGNAL == signal.SIGTERM


# ---- guarded launch -------------------------------------------------------------

def test_guarded_launch_refuses_old_oneshot():
    fake = FakeSystemActions()
    r = guarded_launch(launcher(fmt="oneshot"), fake, DEFAULT)
    assert r == ActionResult(
        False, "old one-shot launcher; rebuild it with new-claude before launching from here")
    assert fake.launched == []


def test_guarded_launch_refuses_pre_argless_launcher_on_default_with_argless_running():
    fake = FakeSystemActions(ps=[ProcessLine(504, BIN)], alive={504})
    r = guarded_launch(launcher(fmt="watchdog", data_dir=DEFAULT), fake, DEFAULT, rearm=True)
    assert not r.ok and "argless" in r.message and fake.launched == []


def test_guarded_launch_rereads_ps_and_refuses_running():
    target = launcher()
    fake = FakeSystemActions(ps=[main_line(501, target.data_dir)], alive={501})
    r = guarded_launch(target, fake, DEFAULT)
    assert r == ActionResult(False, "already running")
    assert fake.calls[0] == ("processes",) and fake.launched == []


def test_guarded_launch_refuses_when_ps_cannot_be_read():
    fake = FakeSystemActions(before_call=ps_fails)
    r = guarded_launch(launcher(), fake, DEFAULT)
    assert not r.ok and fake.launched == []
    assert calls_named(fake, "launch") == []


def test_guarded_launch_opens_launcher_when_clear():
    target = launcher()
    fake = FakeSystemActions(ps=[main_line(700, Path("/Support/Other"))])
    assert guarded_launch(target, fake, DEFAULT).ok
    assert fake.launched == [target.app_path]


def test_rearm_launches_through_the_launcher_while_running():
    st = state_running([501])
    fake = FakeSystemActions(ps=[main_line(501, st.launcher.data_dir)], alive={501})
    assert guarded_launch(st.launcher, fake, DEFAULT, rearm=True).ok
    assert fake.launched == [st.launcher.app_path]


# ---- launch all ---------------------------------------------------------------

def test_launch_all_order_skip_and_gap():
    slept: list[float] = []
    fake = FakeSystemActions()
    results = launch_all(
        snapshot(rows=[closed("B"), running("A"), closed("C"), duplicate("D")]),
        fake, DEFAULT, sleep=slept.append)
    assert [n for n, r in results if r.ok] == ["B", "C"]
    assert slept == [LAUNCH_GAP_S] == [3]
    assert fake.launched == [Path("/Apps/B.app"), Path("/Apps/C.app")]


def test_launch_all_uses_given_launch_one_and_no_gap_after_failure():
    slept: list[float] = []
    tried: list[str] = []

    def launch_one(l: Launcher) -> ActionResult:
        tried.append(l.name)
        return ActionResult(l.name != "B", "x")

    launch_all(snapshot(rows=[closed("B"), closed("C"), closed("E")]),
               FakeSystemActions(), DEFAULT, sleep=slept.append, launch_one=launch_one)
    assert tried == ["B", "C", "E"] and slept == [3]


# ---- system actions -------------------------------------------------------------

def test_launch_and_front_argv():
    assert MacSystemActions().launch_argv(Path("/Apps/X.app")) == ["open", "-n", "/Apps/X.app"]
    argv = " ".join(MacSystemActions().front_argv(501))
    assert "unix id is 501" in argv and 'application "Claude"' not in argv


def test_automation_denied_detected():
    assert automation_denied(
        "execution error: Not authorized to send Apple events to System Events. (-1743)")
    assert automation_denied("osascript is not allowed assistive access.")
    assert not automation_denied("")


# ---- the fake itself ------------------------------------------------------------

def test_fake_records_calls_and_dies_after_polls():
    seen: list[str] = []
    fake = FakeSystemActions(
        ps=[ProcessLine(1, "a")], alive={1}, quit_after=2, before_call=seen.append)
    fake.send_quit(1)
    assert fake.is_alive(1)
    assert fake.processes() == []
    assert not fake.is_alive(1)
    assert seen == ["send_quit", "is_alive", "processes", "is_alive"]
    assert fake.calls[0] == ("send_quit", 1)


def test_fake_sigkill_is_immediate_unless_never_dies():
    fake = FakeSystemActions(alive={1, 2}, never_dies={2})
    fake.signal_pids([1, 2], signal.SIGKILL)
    assert fake.alive == {2}


def test_fake_sigterm_ignored_until_sigkill():
    fake = FakeSystemActions(alive={1}, sigterm_ignored={1}, quit_after=0)
    fake.signal_pids([1], signal.SIGTERM)
    assert fake.is_alive(1)
    fake.signal_pids([1], signal.SIGKILL)
    assert not fake.is_alive(1)


def test_fake_quit_results_fail_without_dying():
    fake = FakeSystemActions(alive={1}, quit_results={1: ActionResult(False, "no")})
    assert fake.send_quit(1) == ActionResult(False, "no")
    assert fake.is_alive(1)


def test_fake_remove_lock_only_when_it_names_pid():
    fake = FakeSystemActions(locks={Path("/L/a.lock"): 5})
    assert not fake.remove_lock_if_names(Path("/L/a.lock"), 6)
    assert fake.remove_lock_if_names(Path("/L/a.lock"), 5) and fake.locks == {}


def test_fake_launch_calls_on_launch():
    hits: list[Path] = []
    fake = FakeSystemActions(on_launch=hits.append)
    assert fake.launch(Path("/Apps/X.app")).ok
    assert hits == fake.launched == [Path("/Apps/X.app")]

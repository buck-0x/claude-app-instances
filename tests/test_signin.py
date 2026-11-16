import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from helpers import FakeSystemActions, ScriptedRefresh

from claude_instances import signin
from claude_instances.actions import ActionResult
from claude_instances.cookies import SessionState
from claude_instances.launchers import Launcher
from claude_instances.processes import MainProcess, ProcessLine, Running
from claude_instances.signin import (
    SigninFlow,
    SigninPhase,
    SigninRefused,
    clear_restore,
    plan_signin,
    read_restore,
    write_restore,
)
from claude_instances.snapshot import InstanceState, Snapshot

CLAUDE_APP = Path("/Applications/FakeClaude.app")
BIN = f"{CLAUDE_APP}/Contents/MacOS/Claude"
DEFAULT = Path("/Support/Claude")
UNKNOWN = Path("/Support/Claude-Unknown")
LOGS = Path("/Logs")  # only the fake's in-memory locks see it
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
A = "11111111-2222-4333-8444-555555555555"
B = "22222222-2222-4333-8444-555555555555"
NAMES = ["P", "C", "T", "D", "W", "O"]
FMT = {"O": "oneshot"}
DATA = {"P": DEFAULT}
WATCHDOG_PID = {"P": 514, "C": 511, "W": 513}
T_PID = 520


def launcher(name: str, fmt: str | None = None) -> Launcher:
    fmt = fmt or FMT.get(name, "argless")
    app = Path(f"/Apps/{name}.app")
    return Launcher(
        name=name,
        app_path=app,
        script_path=app / "Contents/MacOS/launcher",
        data_dir=DATA.get(name, Path(f"/Support/{name}")),
        claude_app=CLAUDE_APP,
        is_watchdog=fmt != "oneshot",
        has_argless_rule=fmt == "argless",
    )


APP = {name: launcher(name).app_path for name in NAMES}


def row(name: str, position: int, running: list[Running], watchdog: bool = False,
        account: str | None = None, created: int | None = None,
        fmt: str | None = None) -> InstanceState:
    return InstanceState(
        name=name,
        launcher=launcher(name, fmt),
        position=position,
        running=running,
        session=SessionState.SIGNED_OUT if created is None else SessionState.SIGNED_IN,
        session_expiry=None,
        session_created=created,
        account=account,
        email=None,
        org=None,
        expected=None,
        shared_with=[],
        expected_mismatch=False,
        usage=None,
        live_error=None,
        watchdog_alive=watchdog,
    )


def runs(*pids: int, argless: bool = False) -> list[Running]:
    return [Running(pid, argless=argless) for pid in pids]


def initial(p_fmt: str | None = None) -> Snapshot:
    rows = [
        row("P", 0, runs(504, argless=True), watchdog=True, fmt=p_fmt),
        row("C", 1, runs(501), watchdog=True),
        row("T", 2, [], account=A),  # closed, signed out with no sessionKey row
        row("D", 3, runs(505, 506)),
        row("W", 4, [], watchdog=True),  # its watchdog waits on an update
        row("O", 5, runs(507)),
    ]
    return Snapshot(rows=rows, strays=[MainProcess(502, UNKNOWN)], taken_at=NOW)


snap0 = initial()
snap_with_old_default_launcher = initial(p_fmt="watchdog")


def after(t_running: list[Running] | None = None, account: str | None = A,
          created: int | None = None, p_running: list[Running] | None = None,
          strays: list[MainProcess] | None = None, p_fmt: str | None = None) -> Snapshot:
    """A snapshot after the quit: everything closed except what is given."""
    rows = []
    for i, name in enumerate(NAMES):
        if name == "T":
            rows.append(row("T", i, runs(T_PID) if t_running is None else t_running,
                            account=account, created=created))
        elif name == "P":
            rows.append(row("P", i, p_running or [], fmt=p_fmt))
        else:
            rows.append(row(name, i, []))
    return Snapshot(rows=rows, strays=strays or [], taken_at=NOW)


ALONE = after()
NOT_RUNNING = after(t_running=[])


def main_line(pid: int, data_dir: Path | None) -> ProcessLine:
    return ProcessLine(pid, BIN if data_dir is None else f"{BIN} --user-data-dir={data_dir}")


def initial_ps() -> list[ProcessLine]:
    lines = [
        main_line(504, None),
        main_line(501, Path("/Support/C")),
        main_line(505, Path("/Support/D")),
        main_line(506, Path("/Support/D")),
        main_line(507, Path("/Support/O")),
        main_line(502, UNKNOWN),
    ]
    for name, pid in WATCHDOG_PID.items():
        lines.append(ProcessLine(pid, f"/bin/zsh {launcher(name).script_path}"))
    return lines


def make_fake() -> FakeSystemActions:
    ps = initial_ps()
    return FakeSystemActions(
        ps=ps, alive={line.pid for line in ps}, quit_after=1,
        locks={LOGS / f"{n}.lock": pid for n, pid in WATCHDOG_PID.items()})


def noop(_s: float) -> None:
    pass


RESTORE = "signin-restore.txt"


@pytest.fixture
def fake() -> FakeSystemActions:
    return make_fake()


@pytest.fixture
def refresh() -> ScriptedRefresh:
    return ScriptedRefresh([ALONE, after(created=5)])


@pytest.fixture
def flow(fake, refresh, tmp_path) -> SigninFlow:
    return SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS)


def restore_names(tmp_path: Path) -> list[str]:
    return read_restore(tmp_path / RESTORE)[1]


# ---- plan ---------------------------------------------------------------------

def test_plan_quits_everything_but_target():
    p = plan_signin(snap0, "T")
    assert set(p.quit_rows) == {"P", "C", "D", "O"} and set(p.quit_pids) == {504, 501, 505, 506, 507, 502}
    assert set(p.watchdog_rows) == {"P", "C", "W"} and p.restore == ["P", "C", "W"]
    assert p.not_restorable == ["D (duplicate; pids 505, 506)", "O (old one-shot launcher)", "Claude-Unknown (pid 502)"]


def test_plan_target_default_keeps_its_argless_pid():
    assert 504 not in plan_signin(snap0, "P").quit_pids


def test_plan_refuses_duplicate_target():
    with pytest.raises(SigninRefused, match="more than one process"):
        plan_signin(snap0, "D")


def test_plan_refuses_closed_one_shot_target():
    snap = initial()
    snap.rows[5] = row("O", 5, [])
    with pytest.raises(SigninRefused, match="old one-shot launcher"):
        plan_signin(snap, "O")


def test_plan_lists_argless_stray():
    snap = initial()
    snap.strays.append(MainProcess(509, None))
    assert plan_signin(snap, "T").not_restorable[-1] == "argless Claude (pid 509)"


# ---- restore file ---------------------------------------------------------------

def test_restore_file_round_trip(tmp_path):
    path = tmp_path / "sub" / RESTORE
    assert read_restore(path) == (None, [])
    write_restore(path, ["P", "Claude C"], owner_pid=4242)
    assert path.read_text() == "owner 4242\nP\nClaude C\n"
    assert read_restore(path) == (4242, ["P", "Claude C"])
    clear_restore(path)
    clear_restore(path)  # already gone is fine
    assert not path.exists()


# ---- flow -----------------------------------------------------------------------

def test_restore_file_written_before_any_quit(tmp_path, fake, flow):
    seen = []
    fake.before_call = lambda name: seen.append((name, restore_names(tmp_path)))
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    first_quit = next(s for s in seen if s[0] in ("signal_pids", "send_quit"))
    assert first_quit[1] == ["P", "C", "W"] and r.ok


def test_unwritable_restore_file_quits_nothing(tmp_path, fake, refresh):
    blocker = tmp_path / "file"
    blocker.write_text("")
    flow = SigninFlow(fake, refresh, blocker, DEFAULT, sleep=noop, log_dir=LOGS)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert not r.ok and "restore file" in r.message
    assert fake.calls == [] and refresh.calls == 0


def test_signed_out_target_without_cookie_row(tmp_path, fake):
    refresh = ScriptedRefresh([ALONE, ALONE, after(created=5)])
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert r.ok and r.message != "sign-in failed (TypeError)" and refresh.calls == 3


def test_success_restores_in_order_and_clears(tmp_path, fake, flow):
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert r.ok and r.restored == ["P", "C", "W"] and r.restore_failed == []
    assert fake.launched == [APP["T"], APP["P"], APP["C"], APP["W"]] and not (tmp_path / RESTORE).exists()


def test_quit_stops_watchdogs_and_clears_sigkilled_locks_first(tmp_path):
    fake = make_fake()
    fake.sigterm_ignored = {514}
    flow = SigninFlow(fake, ScriptedRefresh([ALONE, after(created=5)]), tmp_path, DEFAULT,
                      sleep=noop, log_dir=LOGS)
    assert flow.run(plan_signin(snap0, "T"), threading.Event()).ok
    first_quit = fake.calls.index(next(c for c in fake.calls if c[0] == "send_quit"))
    assert ("signal_pids", [511, 513, 514], signal.SIGTERM) in fake.calls[:first_quit]
    assert ("signal_pids", [514], signal.SIGKILL) in fake.calls[:first_quit]
    assert LOGS / "P.lock" not in fake.locks


def test_success_on_account_change(tmp_path, fake):
    refresh = ScriptedRefresh([ALONE, after(account=B)])
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert r.ok and r.new_account == B


def test_cancel_restores(tmp_path, fake):
    ev = threading.Event()
    # call 1 still shows T starting, call 2 shows it alone, call 3 is the first WAITING poll
    refresh = ScriptedRefresh([NOT_RUNNING, ALONE], on_call={3: ev.set})
    phases = []
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS,
                      on_phase=lambda p, m: phases.append((p, refresh.calls)))
    r = flow.run(plan_signin(snap0, "T"), ev)
    assert (r.ok, r.message, r.restored) == (False, "cancelled", ["P", "C", "W"]) and not (tmp_path / RESTORE).exists()
    assert (SigninPhase.WAITING_FOR_LOGIN, 2) in phases and refresh.calls == 3


def test_exception_during_wait_still_restores(tmp_path, fake):
    refresh = ScriptedRefresh([ALONE, RuntimeError("x")])
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert r.message == "sign-in failed (RuntimeError)" and r.restored == ["P", "C", "W"]
    assert not (tmp_path / RESTORE).exists()


def test_quit_timeout_fails_rearms_survivor_and_restores(tmp_path, fake, refresh, flow):
    fake.never_dies = {501}
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert not r.ok and r.message == "did not quit: C" and APP["T"] not in fake.launched
    assert r.restored == ["P", "C", "W"] and APP["C"] in fake.launched     # C re-armed, not left without a watchdog
    assert refresh.calls == 0


def test_stuck_watchdog_fails_before_quitting_and_restores(tmp_path, fake, flow):
    fake.never_dies = {511}
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert not r.ok and r.message == "watchdog did not stop: C"
    assert not any(c[0] == "send_quit" for c in fake.calls)  # nothing quit with a watchdog alive
    assert r.restored == ["P", "C", "W"] and APP["P"] in fake.launched  # P still running: re-armed


def test_quit_skips_snapshot_pid_no_longer_claude(tmp_path, fake, flow):
    fake.ps = [line for line in fake.ps if line.pid != 507] + [ProcessLine(507, "/usr/bin/vim")]
    assert flow.run(plan_signin(snap0, "T"), threading.Event()).ok
    assert ("send_quit", 507) not in fake.calls


def test_restore_refusal_kept_in_restore_file(tmp_path, fake):
    def shipit(app: Path) -> None:  # an argless Claude appears once T is opened
        if app == APP["T"]:
            fake.ps.append(main_line(600, None))
            fake.alive.add(600)
    fake.on_launch = shipit
    refresh = ScriptedRefresh([after(p_fmt="watchdog"), after(created=5, p_fmt="watchdog")])
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS)
    r = flow.run(plan_signin(snap_with_old_default_launcher, "T"), threading.Event())
    assert [n for n, _ in r.restore_failed] == ["P"] and restore_names(tmp_path) == ["P"]
    assert r.restored == ["C", "W"]


def test_target_launch_refused_fails_immediately(tmp_path, monkeypatch, fake, refresh, flow):
    real = signin.guarded_launch
    monkeypatch.setattr(signin, "guarded_launch", lambda l, *a, **k: ActionResult(False, "refused for test") if l.name == "T" else real(l, *a, **k))
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert not r.ok and "refused for test" in r.message and refresh.calls == 0 and r.restored == ["P", "C", "W"]


def test_target_launch_already_pending_waits_for_it(tmp_path, fake, refresh):
    asked = []

    def pending(l: Launcher) -> ActionResult:
        asked.append(l.name)
        return ActionResult(False, "launch requested 3s ago; waiting for it to appear")
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS,
                      launch_one=pending)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert r.ok and asked == ["T"] and APP["T"] not in fake.launched


def test_target_did_not_start(tmp_path, fake):
    refresh = ScriptedRefresh([NOT_RUNNING])
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert (r.ok, r.message) == (False, "T did not start") and refresh.calls == 31
    assert r.restored == ["P", "C", "W"]


def test_argless_appearing_while_launching_is_quit_before_waiting(tmp_path, fake):
    fake.ps.append(main_line(600, None))
    fake.alive.add(600)
    refresh = ScriptedRefresh([after(p_running=runs(600, argless=True)), ALONE, after(created=5)])
    phases = []
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS,
                      on_phase=lambda p, m: phases.append((p, refresh.calls)))
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert ("send_quit", 600) in fake.calls and any("pid 600" in w for w in r.warnings)
    first_wait = next(c for p, c in phases if p is SigninPhase.WAITING_FOR_LOGIN)
    alone = refresh.returned[first_wait - 1]
    assert first_wait >= 2 and alone.strays == [] and [r.name for r in alone.rows if r.pids] == ["T"]


def test_stray_appearing_while_launching_is_quit_and_reported(tmp_path, fake):
    fake.ps.append(main_line(601, UNKNOWN))
    fake.alive.add(601)
    refresh = ScriptedRefresh([after(strays=[MainProcess(601, UNKNOWN)]), ALONE, after(created=5)])
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert r.ok and ("send_quit", 601) in fake.calls
    assert r.warnings == ["quit Claude-Unknown (pid 601) that started during sign-in"]


def test_reopened_instance_is_added_to_restore_list(tmp_path, fake):
    fake.ps.append(main_line(602, Path("/Support/C")))
    fake.alive.add(602)
    plan = plan_signin(snap0, "T")
    plan.restore = ["P", "W"]  # as if C had not been running when the sign-in started
    reopened = after()
    reopened.rows[1] = row("C", 1, runs(602))
    seen = []
    refresh = ScriptedRefresh([reopened, ALONE, after(created=5)],
                              on_call={2: lambda: seen.append(restore_names(tmp_path))})
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS)
    r = flow.run(plan, threading.Event())
    assert seen == [["P", "C", "W"]] and r.restored == ["P", "C", "W"]


def test_waiting_only_after_target_running(tmp_path, fake):
    refresh = ScriptedRefresh([NOT_RUNNING, NOT_RUNNING, ALONE, after(created=5)])
    phases = []
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS,
                      on_phase=lambda p, m: phases.append((p, refresh.calls)))
    flow.run(plan_signin(snap0, "T"), threading.Event())
    assert next(c for p, c in phases if p is SigninPhase.WAITING_FOR_LOGIN) >= 3


def test_new_main_process_during_wait_warns(tmp_path, fake):
    p601 = runs(601, argless=True)
    refresh = ScriptedRefresh([ALONE, after(p_running=p601), after(p_running=p601, created=5)])
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert r.warnings == ["another Claude started (pid 601); the login may land there"]


def test_running_target_is_not_launched(tmp_path, fake):
    snap = initial()
    snap.rows[2] = row("T", 2, runs(T_PID), account=A)
    fake.ps.append(main_line(T_PID, Path("/Support/T")))
    fake.alive.add(T_PID)
    refresh = ScriptedRefresh([ALONE, after(created=5)])
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS)
    r = flow.run(plan_signin(snap, "T"), threading.Event())
    assert r.ok and APP["T"] not in fake.launched and ("send_quit", T_PID) not in fake.calls


def test_isolating_gives_up_when_others_keep_starting(tmp_path, fake):
    fake.ps.append(main_line(601, UNKNOWN))
    fake.alive.add(601)
    refresh = ScriptedRefresh([after(strays=[MainProcess(601, UNKNOWN)])])  # never alone
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert (r.ok, r.message) == (False, "other Claude processes kept starting; sign-in stopped")
    assert refresh.calls == 1 + signin.ISOLATE_ROUNDS and r.restored == ["P", "C", "W"]


# ---- review fixes ---------------------------------------------------------------

def test_failing_phase_callback_never_stops_the_flow_or_the_restore(tmp_path, fake):
    def on_phase(phase: SigninPhase, _message: str) -> None:
        if phase in (SigninPhase.WAITING_FOR_LOGIN, SigninPhase.RESTORING, SigninPhase.DONE):
            raise RuntimeError("app has stopped")
    refresh = ScriptedRefresh([ALONE, after(created=5)])
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS,
                      on_phase=on_phase)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert r.ok and r.restored == ["P", "C", "W"]
    assert fake.launched == [APP["T"], APP["P"], APP["C"], APP["W"]]
    assert not (tmp_path / RESTORE).exists()


def test_isolating_warns_only_for_pids_it_quit(tmp_path, fake):
    gone = MainProcess(603, UNKNOWN)  # in the snapshot, already gone from ps
    refresh = ScriptedRefresh([after(strays=[gone]), ALONE, after(created=5)])
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert r.ok and r.warnings == [] and ("send_quit", 603) not in fake.calls


def test_target_started_since_the_plan_is_not_launched(tmp_path, fake, flow):
    fake.ps.append(main_line(T_PID, Path("/Support/T")))
    fake.alive.add(T_PID)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())  # the plan saw T closed
    assert r.ok and APP["T"] not in fake.launched and ("send_quit", T_PID) not in fake.calls


def test_target_stopping_while_waiting_fails_and_restores(tmp_path, fake):
    refresh = ScriptedRefresh([ALONE, NOT_RUNNING])
    flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop, log_dir=LOGS)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert (r.ok, r.message) == (False, "T stopped while waiting for the login")
    assert refresh.calls == 2 and r.restored == ["P", "C", "W"]


def test_failed_quit_request_waits_for_dying_instances_before_relaunch(tmp_path, monkeypatch):
    fake = make_fake()
    fake.quit_after = 4  # asked processes take a few polls to exit
    fake.quit_results = {505: ActionResult(False, "permission denied")}
    rearms = []
    real = signin.guarded_launch

    def spy(launcher, *a, rearm=False, **k):
        rearms.append((launcher.name, rearm))
        return real(launcher, *a, rearm=rearm, **k)

    monkeypatch.setattr(signin, "guarded_launch", spy)
    flow = SigninFlow(fake, ScriptedRefresh([ALONE]), tmp_path, DEFAULT, sleep=noop,
                      log_dir=LOGS)
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert not r.ok and "permission denied" in r.message
    assert rearms == [("P", False), ("C", False), ("W", False)] and r.restored == ["P", "C", "W"]

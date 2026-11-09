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
    rearm_watchdog,
)
from claude_instances.cookies import SessionState
from claude_instances.launchers import Launcher
from claude_instances.processes import ProcessLine, Running
from claude_instances.snapshot import InstanceState, Snapshot

WD = "/Apps/Claude C.app/Contents/MacOS/launcher"
CLAUDE_APP = Path("/Applications/FakeClaude.app")
BIN = f"{CLAUDE_APP}/Contents/MacOS/Claude"
DEFAULT = Path("/Support/Claude")
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


def state(name: str, pids: list[int], script: str | None = None) -> InstanceState:
    return InstanceState(
        name=name,
        launcher=launcher(name, script=script),
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


# ---- quit ---------------------------------------------------------------------

def test_quit_stops_and_waits_for_watchdog_then_quits():
    fake = FakeSystemActions(
        alive={501, 508}, ps=[ProcessLine(508, f"/bin/zsh {WD}"), ProcessLine(501, "claude ...")])
    outcome, _ = quit_instance(state_running([501], script=WD), fake, sleep=no_sleep)
    signals = [c for c in fake.calls if c[0] in ("signal_pids", "send_quit")]
    assert signals[:2] == [("signal_pids", [508], signal.SIGTERM), ("send_quit", 501)]
    w, q = fake.calls.index(signals[0]), fake.calls.index(signals[1])
    assert ("processes",) in fake.calls[w + 1:q]  # watchdog exit confirmed by a ps poll first
    assert outcome is QuitOutcome.QUIT


def test_stuck_watchdog_escalates_then_aborts():
    fake = FakeSystemActions(
        alive={501, 508},
        ps=[ProcessLine(508, f"/bin/zsh {WD}"), ProcessLine(501, "claude ...")],
        never_dies={508},
    )
    outcome, message = quit_instance(state_running([501], script=WD), fake, sleep=no_sleep)
    assert (outcome, message) == (
        QuitOutcome.WATCHDOG_STUCK, "watchdog did not stop; instance left running")
    assert ("signal_pids", [508], signal.SIGKILL) in fake.calls
    assert ("send_quit", 501) not in fake.calls


def test_watchdog_stop_waits_up_to_timeout_before_sigkill():
    slept: list[float] = []
    fake = FakeSystemActions(
        alive={508}, ps=[ProcessLine(508, f"/bin/zsh {WD}")], never_dies={508})
    quit_instance(state_running([501], script=WD), fake, sleep=slept.append)
    kill_at = fake.calls.index(("signal_pids", [508], signal.SIGKILL))
    assert sum(slept) == 5 + 2 and set(slept) == {0.5}
    assert fake.calls[kill_at - 1] == ("processes",)


def test_quit_timeout_reports_watchdog_stopped():
    fake = FakeSystemActions(alive={501}, ps=[ProcessLine(501, "claude")], never_dies={501})
    assert quit_instance(state_running([501]), fake, sleep=no_sleep) == (
        QuitOutcome.DID_NOT_QUIT, "did not quit; watchdog stopped")


def test_quit_polls_is_alive_every_second_for_thirty_seconds():
    slept: list[float] = []
    fake = FakeSystemActions(alive={501}, ps=[ProcessLine(501, "claude")], never_dies={501})
    quit_instance(state_running([501]), fake, sleep=slept.append)
    assert slept == [1] * 30


def test_quit_duplicate_quits_both_pids():
    fake = FakeSystemActions(alive={505, 506}, ps=[ProcessLine(505, "c"), ProcessLine(506, "c")])
    outcome, _ = quit_instance(state_running([505, 506]), fake, sleep=no_sleep)
    assert ("send_quit", 505) in fake.calls and ("send_quit", 506) in fake.calls
    assert outcome is QuitOutcome.QUIT


def test_quit_not_running_touches_nothing():
    fake = FakeSystemActions(ps=[ProcessLine(508, f"/bin/zsh {WD}")], alive={508})
    assert quit_instance(state_running([], script=WD), fake, sleep=no_sleep)[0] is (
        QuitOutcome.NOT_RUNNING)
    assert not [c for c in fake.calls if c[0] in ("signal_pids", "send_quit")]


def test_quit_never_signals_claude_as_a_watchdog():
    fake = FakeSystemActions(
        alive={501, 508},
        ps=[ProcessLine(508, f"/bin/zsh {WD}"),
            ProcessLine(501, f"{BIN} --user-data-dir=/Support/Claude C")],
    )
    quit_instance(state_running([501], script=WD), fake, sleep=no_sleep)
    assert [c for c in fake.calls if c[0] == "signal_pids"] == [
        ("signal_pids", [508], signal.SIGTERM)]


def test_quit_refuses_when_ps_cannot_be_read():
    fake = FakeSystemActions(alive={501, 508}, before_call=ps_fails)
    outcome, message = quit_instance(state_running([501], script=WD), fake, sleep=no_sleep)
    assert outcome is QuitOutcome.WATCHDOG_STUCK and "left running" in message
    assert not [c for c in fake.calls if c[0] in ("signal_pids", "send_quit")]


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
    fake = FakeSystemActions(
        ps=[ProcessLine(501, f"{BIN} --user-data-dir={target.data_dir}")], alive={501})
    r = guarded_launch(target, fake, DEFAULT)
    assert r == ActionResult(False, "already running")
    assert fake.calls[0] == ("processes",) and fake.launched == []


def test_guarded_launch_refuses_when_ps_cannot_be_read():
    fake = FakeSystemActions(before_call=ps_fails)
    r = guarded_launch(launcher(), fake, DEFAULT)
    assert not r.ok and fake.launched == []
    assert not [c for c in fake.calls if c[0] == "launch"]


def test_guarded_launch_opens_launcher_when_clear():
    target = launcher()
    fake = FakeSystemActions(ps=[ProcessLine(700, f"{BIN} --user-data-dir=/Support/Other")])
    assert guarded_launch(target, fake, DEFAULT).ok
    assert fake.launched == [target.app_path]


def test_rearm_launches_through_the_launcher_while_running():
    st = state_running([501])
    fake = FakeSystemActions(
        ps=[ProcessLine(501, f"{BIN} --user-data-dir={st.launcher.data_dir}")], alive={501})
    assert rearm_watchdog(st, fake, DEFAULT).ok
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


def test_fake_launch_calls_on_launch():
    hits: list[Path] = []
    fake = FakeSystemActions(on_launch=hits.append)
    assert fake.launch(Path("/Apps/X.app")).ok
    assert hits == fake.launched == [Path("/Apps/X.app")]

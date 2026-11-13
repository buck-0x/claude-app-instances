"""The Textual app, driven through run_test()/Pilot against fakes only.

Nothing here launches, quits, signals or raises a real process: every system call
goes to `FakeSystemActions`, and sign-ins use `BlockingFlow`.
"""

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers import FakeSystemActions, make_data_dir, make_launcher_app
from textual.widgets import DataTable

from claude_instances.actions import AUTOMATION_HELP, ActionResult
import claude_instances.app as app_module
from claude_instances.app import InstancesApp
from claude_instances.backend import DECRYPT_ERROR, Backend
from claude_instances.paths import RESTORE_FILE
from claude_instances.processes import ProcessLine
from claude_instances.screens import (
    SESSION_CAVEAT,
    ConfirmScreen,
    DetailsScreen,
    RestoreOfferScreen,
    SigninScreen,
)
from claude_instances.signin import SigninResult, read_restore, write_restore
from claude_instances.usage import LookupResult, Source, Usage

A = "11111111-2222-4333-8444-555555555555"
B = "22222222-2222-4333-8444-555555555555"
ORG = "aaaaaaaa-2222-4333-8444-555555555555"
EMAIL = "user@example.com"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
EXPIRY = NOW + timedelta(days=30)
USAGE = Usage(12.0, NOW + timedelta(hours=2), 40.0, NOW + timedelta(days=3), NOW, Source.LIVE)


class FakeLookup:
    """Lookup double: answers by data folder name."""

    def __init__(self, results: dict[str, LookupResult]) -> None:
        self.results = results

    def lookup(self, data_dir: Path) -> LookupResult:
        return self.results[data_dir.name]


class World:
    """Three launchers: C (running 501), Dup (502 and 503 on one folder), I (closed).

    C and Dup are signed in to account A, so they share it; I has no session.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.support = tmp_path / "Support"
        self.claude_app = tmp_path / "FakeClaude.app"
        binary = f"{self.claude_app}/Contents/MacOS/Claude"
        self.apps = tmp_path / "Apps"
        self.cfg = tmp_path / "Config"
        self.log_dir = tmp_path / "Logs"
        self.log_dir.mkdir()
        self.default_data_dir = self.support / "Claude"
        self.dirs = {
            "Claude C": self.support / "Claude-C",
            "Claude Dup": self.support / "Claude-Dup",
            "Claude I": self.support / "Claude-I",
        }
        for name, data_dir in self.dirs.items():
            make_launcher_app(self.apps, name, data_dir, claude_app=self.claude_app,
                              default_data_dir=self.default_data_dir)
        make_data_dir(self.dirs["Claude C"], A, EXPIRY, None)
        make_data_dir(self.dirs["Claude Dup"], A, EXPIRY, None)
        make_data_dir(self.dirs["Claude I"], None, "none", None)
        dup = f"{binary} --user-data-dir={self.dirs['Claude Dup']}"
        self.fake = FakeSystemActions(
            ps=[
                ProcessLine(501, f"{binary} --user-data-dir={self.dirs['Claude C']}"),
                ProcessLine(502, dup),
                ProcessLine(503, dup),
            ],
            alive={501, 502, 503},
        )

    def backend(self, lookup: FakeLookup | None = None) -> Backend:
        return Backend(
            apps_dirs=[self.apps], config_dir=self.cfg, log_dir=self.log_dir,
            support_dir=self.support, actions=self.fake, lookup=lookup,
            default_data_dir=self.default_data_dir, default_claude_app=self.claude_app,
            sleep=lambda s: None, now=lambda: NOW,
        )

    def app_path(self, name: str) -> Path:
        return self.apps / f"{name}.app"


class BlockingFlow:
    """A sign-in flow double: blocks until cancelled or released, then "restores"."""

    def __init__(self, result: SigninResult | None = None, hold_restore: bool = False) -> None:
        self.result = result
        # With hold_restore, a cancelled run waits for `restore_gate` before "restoring",
        # so a test can look at the app while the restore is still under way.
        self.restore_gate = threading.Event()
        if not hold_restore:
            self.restore_gate.set()
        self.cancelled = threading.Event()
        self.started = threading.Event()
        self._released = threading.Event()
        self.restored = False

    def release(self) -> None:
        self._released.set()

    def run(self, plan, cancelled: threading.Event) -> SigninResult:
        self.plan = plan
        self.cancelled = cancelled
        self.started.set()
        while not (cancelled.is_set() or self._released.is_set()):
            cancelled.wait(0.01)
        if cancelled.is_set():
            assert self.restore_gate.wait(5)
        self.restored = True
        if cancelled.is_set():
            return SigninResult(False, "cancelled")
        return self.result or SigninResult(True, "signed in")


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
def fake(world: World) -> FakeSystemActions:
    return world.fake


@pytest.fixture
def backend(world: World) -> Backend:
    return world.backend(None)


@pytest.fixture
def backend_with_fake_lookup(world: World) -> Backend:
    ok = LookupResult(usage=USAGE, email=EMAIL, org=ORG)
    return world.backend(FakeLookup({"Claude-C": ok, "Claude-Dup": ok}))


@pytest.fixture
def backend_with_decrypt_errors(world: World) -> Backend:
    bad = LookupResult(usage=None, error=DECRYPT_ERROR)
    return world.backend(FakeLookup({"Claude-C": bad, "Claude-Dup": bad}))


def select(pilot, name: str) -> None:
    table = pilot.app.query_one(DataTable)
    table.move_cursor(row=table.get_row_index(name))


async def start_signin(pilot, name: str) -> BlockingFlow:
    # Each sign-in test replaces make_signin_flow with a lambda returning its flow.
    flow = pilot.app.backend.make_signin_flow(None)
    select(pilot, name)
    await pilot.press("s")
    await pilot.pause()
    assert isinstance(pilot.app.screen, ConfirmScreen)
    await pilot.press("y")
    await pilot.pause()
    assert flow.started.wait(5)
    return flow


async def finish_workers(pilot) -> None:
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


# ---- table and header ----------------------------------------------------------------

async def test_table_renders_rows_and_cached_header(backend):
    async with InstancesApp(backend).run_test() as pilot:
        table = pilot.app.query_one(DataTable)
        assert table.row_count == 3 and "cached only" in pilot.app.header_text
        assert "DUPLICATE" in str(table.get_row("Claude Dup")[2])
        assert "shared with" in str(table.get_row("Claude C")[3])
        assert "● open" in str(table.get_row("Claude C")[2])
        assert "○ closed" in str(table.get_row("Claude I")[2])
        assert "refreshed " in pilot.app.header_text


async def test_live_lookup_marks_row_live(backend_with_fake_lookup):
    async with InstancesApp(backend_with_fake_lookup).run_test() as pilot:
        assert "live usage: checking key" in pilot.app.header_text
        pilot.app.live_tick()
        await finish_workers(pilot)
        cell = str(pilot.app.query_one(DataTable).get_row("Claude C")[4])
        assert cell.startswith(" 12%") and "old" not in cell
        assert "live usage ✓" in pilot.app.header_text


async def test_live_tick_clears_in_flight_when_the_worker_cannot_start(
        backend_with_fake_lookup, monkeypatch):
    async with InstancesApp(backend_with_fake_lookup).run_test() as pilot:
        await pilot.pause()

        def refuse(*args, **kwargs):
            raise RuntimeError("no worker")
        monkeypatch.setattr(pilot.app, "run_worker", refuse)
        pilot.app.live_tick()
        assert "live lookup failed (RuntimeError)" in pilot.app.status_text
        monkeypatch.undo()
        # The scheduler is not left in flight, so the next lookup can still run.
        assert pilot.app.scheduler.next_due() is not None


async def test_header_reflects_bad_key(backend_with_decrypt_errors):
    async with InstancesApp(backend_with_decrypt_errors).run_test() as pilot:
        pilot.app.live_tick()
        await finish_workers(pilot)
        assert "key cannot decrypt cookies" in pilot.app.header_text
        assert DECRYPT_ERROR in str(pilot.app.query_one(DataTable).get_row("Claude C")[3])


async def test_refresh_failure_is_reported_not_empty(backend, fake):
    async with InstancesApp(backend).run_test() as pilot:
        def fail(name: str) -> None:
            if name == "processes":
                raise OSError("ps failed")
        fake.before_call = fail
        pilot.app.refresh_state()
        assert pilot.app.status_text == "refresh failed (OSError)"
        assert pilot.app.query_one(DataTable).row_count == 3


# ---- actions ---------------------------------------------------------------------------

async def test_launch_key_on_closed_row_calls_open_n(backend, fake, world):
    async with InstancesApp(backend).run_test() as pilot:
        select(pilot, "Claude I")
        await pilot.press("l")
        await finish_workers(pilot)
        assert fake.launched == [world.app_path("Claude I")]


async def test_launch_key_on_running_row_is_refused_with_message(backend, fake):
    async with InstancesApp(backend).run_test() as pilot:
        select(pilot, "Claude C")
        await pilot.press("l")
        await finish_workers(pilot)
        assert fake.launched == [] and "already running" in pilot.app.status_text


async def test_launch_all_reports_skips_as_skips(backend, fake, world):
    async with InstancesApp(backend).run_test() as pilot:
        await pilot.press("a")
        await finish_workers(pilot)
        assert fake.launched == [world.app_path("Claude I")]
        status = pilot.app.status_text
        assert "launched Claude I" in status and "skipped" in status
        assert "failed" not in status


async def test_front_on_closed_row_offers_launch(backend, fake):
    async with InstancesApp(backend).run_test() as pilot:
        select(pilot, "Claude I")
        await pilot.press("f")
        await finish_workers(pilot)
        assert pilot.app.status_text == "Claude I is not running; press l to launch"
        assert not [c for c in fake.calls if c[0] == "bring_to_front"]


async def test_front_denied_shows_help(backend, fake):
    fake.front_result = ActionResult(False, AUTOMATION_HELP)
    async with InstancesApp(backend).run_test() as pilot:
        select(pilot, "Claude C")
        await pilot.press("f")
        await finish_workers(pilot)
        assert ("bring_to_front", 501) in fake.calls
        assert AUTOMATION_HELP in pilot.app.status_text


async def test_quit_asks_then_quits_off_loop(backend, fake):
    async with InstancesApp(backend).run_test() as pilot:
        select(pilot, "Claude C")
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ConfirmScreen)
        assert not [c for c in fake.calls if c[0] == "send_quit"]
        await pilot.press("y")
        await finish_workers(pilot)
        assert ("send_quit", 501) in fake.calls
        assert pilot.app.status_text == "Claude C: quit"


async def test_quit_that_fails_offers_rearm(backend, fake):
    fake.never_dies = {501}
    async with InstancesApp(backend).run_test() as pilot:
        select(pilot, "Claude C")
        await pilot.press("q")
        await pilot.press("y")
        await finish_workers(pilot)
        assert "did not quit" in pilot.app.status_text and "press w" in pilot.app.status_text


async def test_action_exception_becomes_status(backend, fake):
    async with InstancesApp(backend).run_test() as pilot:
        select(pilot, "Claude I")

        def fail(name: str) -> None:
            if name == "processes":
                raise RuntimeError("boom")
        fake.before_call = fail
        await pilot.press("a")
        await finish_workers(pilot)
        assert pilot.app.status_text == (
            "launch all failed (RuntimeError); refresh failed (RuntimeError)")
        assert pilot.app.is_running


async def test_details_shows_rebuild_command_log_and_caveat(backend, world):
    (world.log_dir / "Claude C.log").write_text("".join(f"line {i}\n" for i in range(30)))
    async with InstancesApp(backend).run_test() as pilot:
        select(pilot, "Claude C")
        await pilot.press("d")
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, DetailsScreen)
        assert "new-claude 'Claude C' --dir Claude-C" in screen.text
        assert SESSION_CAVEAT in screen.text
        assert "line 29" in screen.text and "line 9\n" not in screen.text
        await pilot.press("escape")
        assert not isinstance(pilot.app.screen, DetailsScreen)


async def test_move_row_rewrites_order_file(backend, world):
    async with InstancesApp(backend).run_test() as pilot:
        select(pilot, "Claude C")
        await pilot.press("shift+down")
        await pilot.pause()
        assert (world.cfg / "order.txt").read_text() == "Claude Dup\nClaude C\nClaude I\n"
        table = pilot.app.query_one(DataTable)
        assert table.get_row_index("Claude C") == 1 and table.cursor_row == 1


async def test_restore_offer_on_pending_file(backend, world, fake):
    write_restore(world.cfg / RESTORE_FILE, ["Claude I"], owner_pid=99999)
    async with InstancesApp(backend).run_test() as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen, RestoreOfferScreen)
        await pilot.press("y")
        await finish_workers(pilot)
        assert fake.launched == [world.app_path("Claude I")]
        assert not (world.cfg / RESTORE_FILE).exists()


async def test_unreadable_restore_file_is_reported(backend, world):
    world.cfg.mkdir()
    (world.cfg / RESTORE_FILE).write_bytes(b"\xff\xfe\xfa")
    async with InstancesApp(backend).run_test() as pilot:
        await pilot.pause()
        assert "restore file" in pilot.app.status_text and pilot.app.is_running


# ---- sign-in ---------------------------------------------------------------------------

async def test_action_keys_blocked_during_signin(backend, fake):
    flow = BlockingFlow()
    backend.make_signin_flow = lambda on_phase: flow
    async with InstancesApp(backend).run_test() as pilot:
        await start_signin(pilot, "Claude I")
        launched_before = list(fake.launched)
        await pilot.press("a")
        await pilot.press("l")
        await pilot.pause()
        assert fake.launched == launched_before
        # The sign-in modal swallows keys anyway, so check the screen's gate directly.
        screen = pilot.app.main_screen
        for action in ("front", "launch", "quit_instance", "rearm", "launch_all", "signin",
                       "mark_expected", "move_up", "move_down"):
            assert screen.check_action(action, ()) is False, action
        assert screen.check_action("details", ()) is True
        flow.release()
        pilot.app.finish_signin()
        await pilot.pause()
        assert screen.check_action("launch", ()) is True


async def test_exit_during_signin_restores_first(backend):
    flow = BlockingFlow(hold_restore=True)
    backend.make_signin_flow = lambda on_phase: flow
    async with InstancesApp(backend).run_test() as pilot:
        await start_signin(pilot, "Claude I")
        await pilot.press("ctrl+q")
        await pilot.pause()
        assert flow.cancelled.is_set() and pilot.app.is_running
        assert "Restoring" in pilot.app.status_text
        flow.restore_gate.set()
        pilot.app.signin_thread.join(5)
        await pilot.pause()
        await pilot.pause()
        assert flow.restored and not pilot.app.is_running


async def test_builtin_quit_routes_through_request_exit(backend):
    flow = BlockingFlow(hold_restore=True)
    backend.make_signin_flow = lambda on_phase: flow
    async with InstancesApp(backend).run_test() as pilot:
        await start_signin(pilot, "Claude I")
        pilot.app.action_quit()
        await pilot.pause()
        assert flow.cancelled.is_set() and pilot.app.is_running
        flow.restore_gate.set()
        pilot.app.finish_signin(timeout=5)


async def test_finish_signin_cancels_and_joins(backend):
    flow = BlockingFlow()
    backend.make_signin_flow = lambda on_phase: flow
    async with InstancesApp(backend).run_test() as pilot:
        await start_signin(pilot, "Claude I")
        pilot.app.finish_signin(timeout=5)
        assert flow.restored and not pilot.app.signin_thread.is_alive()


async def test_signin_offers_expected_account(backend, world):
    (world.dirs["Claude I"] / "config.json").write_text(json.dumps({"lastKnownAccountUuid": B}))
    flow = BlockingFlow(SigninResult(True, "signed in", new_account=B))
    backend.make_signin_flow = lambda on_phase: flow
    async with InstancesApp(backend).run_test() as pilot:
        await start_signin(pilot, "Claude I")
        flow.release()
        pilot.app.signin_thread.join(5)
        await pilot.pause()
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ConfirmScreen) and B[:8] in screen.question
        await pilot.press("y")
        await pilot.pause()
        expected = json.loads((world.cfg / "expected-accounts.json").read_text())
        assert expected == {"Claude I": B}


async def test_signin_screen_removed_even_under_another_screen(backend):
    flow = BlockingFlow()
    backend.make_signin_flow = lambda on_phase: flow
    async with InstancesApp(backend).run_test() as pilot:
        await start_signin(pilot, "Claude I")
        pilot.app.push_screen(ConfirmScreen("something on top"))
        await pilot.pause()
        flow.release()
        pilot.app.signin_thread.join(5)
        await pilot.pause()
        await pilot.pause()
        assert not any(isinstance(s, SigninScreen) for s in pilot.app.screen_stack)
        assert pilot.app.screen is pilot.app.main_screen and not pilot.app.signin_active


async def test_signin_refused_while_an_action_runs(backend, fake, world):
    gate = threading.Event()
    fake.on_launch = lambda app_path: gate.wait(5)
    flow = BlockingFlow()
    backend.make_signin_flow = lambda on_phase: flow
    async with InstancesApp(backend).run_test() as pilot:
        await pilot.press("a")  # launch all blocks inside launching Claude I
        await pilot.pause()
        select(pilot, "Claude C")
        await pilot.press("s")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, ConfirmScreen)
        assert "still running" in pilot.app.status_text
        assert pilot.app.signin_thread is None
        gate.set()
        await finish_workers(pilot)
        assert fake.launched == [world.app_path("Claude I")]


async def test_signin_carries_a_pending_restore_list(backend, world):
    write_restore(world.cfg / RESTORE_FILE, ["Claude I"], owner_pid=os.getpid())
    flow = BlockingFlow()
    backend.make_signin_flow = lambda on_phase: flow
    async with InstancesApp(backend).run_test() as pilot:
        await pilot.pause()
        if isinstance(pilot.app.screen, RestoreOfferScreen):
            await pilot.press("n")  # declined: the list stays pending
            await pilot.pause()
        select(pilot, "Claude C")
        await pilot.press("s")
        await pilot.pause()
        assert "Claude I" in pilot.app.screen.question
        await pilot.press("y")
        await pilot.pause()
        assert flow.started.wait(5)
        assert flow.plan.restore == ["Claude I"]
        flow.release()
        pilot.app.finish_signin(timeout=5)


async def test_signin_refused_while_another_tui_owns_the_restore_file(backend, world, fake):
    write_restore(world.cfg / RESTORE_FILE, ["Claude I"], owner_pid=4242)
    fake.alive.add(4242)
    async with InstancesApp(backend).run_test() as pilot:
        await pilot.pause()
        assert not isinstance(pilot.app.screen, RestoreOfferScreen)
        select(pilot, "Claude C")
        await pilot.press("s")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, ConfirmScreen)
        assert "another sign-in (pid 4242)" in pilot.app.status_text


async def test_restore_counts_running_instances_as_restored(backend, world, fake):
    write_restore(world.cfg / RESTORE_FILE, ["Claude C", "Claude I"], owner_pid=99999)
    async with InstancesApp(backend).run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await finish_workers(pilot)
        assert fake.launched == [world.app_path("Claude I")]
        assert not (world.cfg / RESTORE_FILE).exists()
        assert "already running: Claude C" in pilot.app.status_text


async def test_restore_keeps_only_refused_names(backend, world, fake):
    write_restore(world.cfg / RESTORE_FILE, ["Claude I"], owner_pid=99999)
    async with InstancesApp(backend).run_test() as pilot:
        await pilot.pause()
        backend.pending_launches["Claude I"] = backend.monotonic()  # a launch just requested
        await pilot.press("y")
        await finish_workers(pilot)
        assert fake.launched == []
        assert read_restore(world.cfg / RESTORE_FILE)[1] == ["Claude I"]
        assert "kept for next start" in pilot.app.status_text


async def test_restore_reports_relaunches_when_the_file_cannot_be_updated(
        backend, world, fake, monkeypatch):
    write_restore(world.cfg / RESTORE_FILE, ["Claude I"], owner_pid=99999)

    def fail(path):
        raise PermissionError("read-only")
    monkeypatch.setattr(app_module, "clear_restore", fail)
    async with InstancesApp(backend).run_test() as pilot:
        await pilot.pause()
        await pilot.press("y")
        await finish_workers(pilot)
        status = pilot.app.status_text
        assert "relaunched Claude I" in status
        assert "could not update the restore file (PermissionError)" in status


async def test_display_error_becomes_status(backend, monkeypatch):
    async with InstancesApp(backend).run_test() as pilot:
        def boom(row, now):
            raise ValueError("bad cell")
        monkeypatch.setattr(app_module, "row_cells", boom)
        assert pilot.app.refresh_state() is False
        assert pilot.app.status_text == "display failed (ValueError)"
        await pilot.pause()
        assert pilot.app.is_running


async def test_action_handler_error_becomes_status(backend, monkeypatch):
    async with InstancesApp(backend).run_test() as pilot:
        def boom(*args):
            raise KeyError("x")
        monkeypatch.setattr(app_module, "details_text", boom)
        select(pilot, "Claude C")
        await pilot.press("d")
        await pilot.pause()
        assert pilot.app.status_text == "details failed (KeyError)" and pilot.app.is_running

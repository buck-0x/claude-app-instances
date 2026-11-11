import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers import FakeSystemActions, make_data_dir, make_launcher_app

from claude_instances.accounts import ExpectedAccounts
from claude_instances.actions import ActionResult, QuitOutcome
from claude_instances.backend import PENDING_LAUNCH_S, Backend
from claude_instances.cookies import SessionState
from claude_instances.processes import ProcessLine
from claude_instances.signin import SigninFlow, write_restore
from claude_instances.snapshot import InstanceState, Snapshot
from claude_instances.usage import LookupResult, Source, Usage, UsageCache

A = "11111111-2222-4333-8444-555555555555"
B = "22222222-2222-4333-8444-555555555555"
ORG = "aaaaaaaa-2222-4333-8444-555555555555"
EMAIL = "user@example.com"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
EXPIRY = NOW + timedelta(days=30)
DECRYPT_ERROR = "cannot decrypt cookies (wrong key?)"
USAGE = Usage(12.0, NOW + timedelta(hours=2), 40.0, NOW + timedelta(days=3), NOW, Source.LIVE)


class FakeLookup:
    """Lookup double: answers by data folder name, recording each folder asked for."""

    def __init__(self, results: dict[str, LookupResult]) -> None:
        self.results = results
        self.asked: list[Path] = []

    def lookup(self, data_dir: Path) -> LookupResult:
        self.asked.append(data_dir)
        return self.results[data_dir.name]


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class World:
    """Three launchers: P (default folder, argless 504), C (running 501), I (closed).

    P and C are signed in to account A; I has no session.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.support = tmp_path / "Support"
        self.claude_app = tmp_path / "FakeClaude.app"
        self.binary = f"{self.claude_app}/Contents/MacOS/Claude"
        self.apps = tmp_path / "Apps"
        self.cfg = tmp_path / "Config"
        self.log_dir = tmp_path / "Logs"
        self.log_dir.mkdir()
        self.dirs = {
            "Claude P": self.support / "Claude",
            "Claude C": self.support / "Claude-C",
            "Claude I": self.support / "Claude-I",
        }
        for name, data_dir in self.dirs.items():
            make_launcher_app(self.apps, name, data_dir, claude_app=self.claude_app,
                              default_data_dir=self.dirs["Claude P"])
        make_data_dir(self.dirs["Claude P"], A, EXPIRY, None)
        make_data_dir(self.dirs["Claude C"], A, EXPIRY, None)
        make_data_dir(self.dirs["Claude I"], None, "none", None)
        self.fake = FakeSystemActions(ps=[
            ProcessLine(504, self.binary),
            ProcessLine(501, f"{self.binary} --user-data-dir={self.dirs['Claude C']}"),
        ])
        self.clock = Clock()

    def backend(self, lookup: FakeLookup | None) -> Backend:
        return Backend(
            apps_dirs=[self.apps], config_dir=self.cfg, log_dir=self.log_dir,
            support_dir=self.support, actions=self.fake, lookup=lookup,
            default_data_dir=self.dirs["Claude P"], default_claude_app=self.claude_app,
            sleep=lambda s: None, now=lambda: NOW, monotonic=self.clock,
        )


def ok_result() -> LookupResult:
    return LookupResult(usage=USAGE, email=EMAIL, org=ORG)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


@pytest.fixture
def fake_lookup() -> FakeLookup:
    return FakeLookup({"Claude-C": ok_result(), "Claude": ok_result()})


@pytest.fixture
def backend(world: World, fake_lookup: FakeLookup) -> Backend:
    return world.backend(fake_lookup)


def row_of(snapshot: Snapshot, name: str) -> InstanceState:
    return snapshot.row(name)


def write_config(data_dir: Path, account: str) -> None:
    (data_dir / "config.json").write_text(json.dumps({"lastKnownAccountUuid": account}))


# ---- construction ------------------------------------------------------------------

def test_cached_only_without_lookup(world: World) -> None:
    b = world.backend(None)
    assert b.cached_only and b.key_status == "none" and b.apps_dirs == [world.apps]
    r = b.live_lookup("Claude C")
    assert r.usage is None and r.error is not None
    assert row_of(b.snapshot(), "Claude C").live_error is None


def test_key_unverified_until_first_result(backend: Backend) -> None:
    assert not backend.cached_only and backend.key_status == "unverified"


# ---- snapshot ----------------------------------------------------------------------

def test_snapshot_rows_in_order(world: World, backend: Backend) -> None:
    (world.cfg).mkdir()
    (world.cfg / "order.txt").write_text("Claude I\n")
    snap = backend.snapshot()
    assert [r.name for r in snap.rows] == ["Claude I", "Claude C", "Claude P"]
    assert row_of(snap, "Claude C").pid == 501 and row_of(snap, "Claude P").argless


def test_snapshot_raises_when_ps_fails(world: World, backend: Backend) -> None:
    def fail(name: str) -> None:
        if name == "processes":
            raise subprocess.CalledProcessError(1, ["ps"])
    world.fake.before_call = fail
    with pytest.raises(subprocess.CalledProcessError):
        backend.snapshot()


def test_corrupt_expected_file_is_reported_not_overwritten(world: World, backend: Backend) -> None:
    world.cfg.mkdir()
    path = world.cfg / "expected-accounts.json"
    path.write_text('["not an object"]')
    snap = backend.snapshot()
    assert row_of(snap, "Claude C").expected is None
    assert backend.expected_error == "expected accounts file unreadable (ValueError)"
    path.write_text("{not json")
    backend.snapshot()
    assert backend.expected_error == "expected accounts file unreadable (JSONDecodeError)"
    r = backend.mark_expected("Claude C")
    assert not r.ok and "unreadable" in r.message
    assert path.read_text() == "{not json"
    path.unlink()
    backend.snapshot()
    assert backend.expected_error is None


# ---- live lookups ------------------------------------------------------------------

def test_live_lookup_success_stores_live_cache_and_info(world: World, backend: Backend,
                                                        fake_lookup: FakeLookup) -> None:
    r = backend.live_lookup("Claude C")
    assert r.usage == USAGE and fake_lookup.asked == [world.dirs["Claude C"]]
    row = row_of(backend.snapshot(), "Claude C")
    assert row.usage.source is Source.LIVE and row.email == "user@example.com"
    assert backend.key_status == "ok"
    assert UsageCache.load(world.cfg / "usage-cache.json").get("Claude C", A) is not None


def test_live_lookup_rejected_records_mark(world: World, backend: Backend,
                                           fake_lookup: FakeLookup) -> None:
    fake_lookup.results["Claude-C"] = LookupResult(usage=None, rejected=True)
    backend.live_lookup("Claude C")
    assert row_of(backend.snapshot(), "Claude C").session is SessionState.REJECTED
    # A new session clears the refusal.
    make_data_dir(world.dirs["Claude C"], A, EXPIRY, None, created=2)
    assert row_of(backend.snapshot(), "Claude C").session is SessionState.SIGNED_IN


def test_success_clears_rejected_and_error(backend: Backend, fake_lookup: FakeLookup) -> None:
    fake_lookup.results["Claude-C"] = LookupResult(usage=None, rejected=True)
    backend.live_lookup("Claude C")
    fake_lookup.results["Claude-C"] = LookupResult(usage=None, error="live unavailable (403)")
    backend.live_lookup("Claude C")
    assert row_of(backend.snapshot(), "Claude C").live_error == "live unavailable (403)"
    fake_lookup.results["Claude-C"] = ok_result()
    backend.live_lookup("Claude C")
    row = row_of(backend.snapshot(), "Claude C")
    assert row.session is SessionState.SIGNED_IN and row.live_error is None


def test_live_lookup_cache_write_error_is_contained(backend: Backend, monkeypatch) -> None:
    monkeypatch.setattr(UsageCache, "save", lambda self, p: (_ for _ in ()).throw(OSError("disk")))
    backend.live_lookup("Claude C")
    row = row_of(backend.snapshot(), "Claude C")
    assert row.live_error == "cache write failed (OSError)"
    assert row.usage.source is Source.LIVE


def test_live_lookup_exception_is_contained(backend: Backend, fake_lookup: FakeLookup) -> None:
    def boom(data_dir: Path) -> LookupResult:
        raise RuntimeError(EMAIL)
    fake_lookup.lookup = boom
    r = backend.live_lookup("Claude C")
    assert r.error == "lookup failed (RuntimeError)" and EMAIL not in repr(r)
    assert row_of(backend.snapshot(), "Claude C").live_error == "lookup failed (RuntimeError)"


def test_live_lookup_unknown_name(backend: Backend, fake_lookup: FakeLookup) -> None:
    r = backend.live_lookup("Nope")
    assert r.usage is None and r.error == "instance read failed (KeyError)"
    assert fake_lookup.asked == []


def test_email_follows_account(world: World, backend: Backend) -> None:
    backend.live_lookup("Claude C")
    write_config(world.dirs["Claude C"], account=B)
    row = row_of(backend.snapshot(), "Claude C")
    assert row.email is None and (row.usage is None or row.usage.source is not Source.LIVE)


def test_key_status_bad_on_decrypt_error(backend: Backend, fake_lookup: FakeLookup) -> None:
    fake_lookup.results = {k: LookupResult(usage=None, error=DECRYPT_ERROR)
                           for k in fake_lookup.results}
    backend.live_lookup("Claude C")
    backend.live_lookup("Claude P")
    assert backend.key_status == "bad"
    assert row_of(backend.snapshot(), "Claude C").live_error == DECRYPT_ERROR


def test_decrypt_error_after_ok_keeps_ok(backend: Backend, fake_lookup: FakeLookup) -> None:
    backend.live_lookup("Claude C")
    fake_lookup.results["Claude"] = LookupResult(usage=None, error=DECRYPT_ERROR)
    backend.live_lookup("Claude P")
    assert backend.key_status == "ok"


def test_account_details_not_in_repr(backend: Backend) -> None:
    backend.live_lookup("Claude C")
    text = repr(backend.snapshot()) + repr(backend.live)
    assert EMAIL not in text and ORG not in text and A not in text


# ---- launch ------------------------------------------------------------------------

def test_pending_launch_blocks_second_launch(world: World, backend: Backend) -> None:
    fake = world.fake
    assert backend.launch("Claude I").ok
    world.clock.t += 5
    r = backend.launch("Claude I")
    assert not r.ok and "waiting for it to appear" in r.message and len(fake.launched) == 1
    assert r.message == "launch requested 5s ago; waiting for it to appear"


def test_pending_launch_expires(world: World, backend: Backend) -> None:
    assert backend.launch("Claude I").ok
    world.clock.t += PENDING_LAUNCH_S
    assert backend.launch("Claude I").ok
    assert len(world.fake.launched) == 2


def test_snapshot_clears_pending_once_running(world: World, backend: Backend) -> None:
    assert backend.launch("Claude I").ok
    world.fake.ps.append(
        ProcessLine(509, f"{world.binary} --user-data-dir={world.dirs['Claude I']}"))
    backend.snapshot()
    assert "Claude I" not in backend.pending_launches


def test_refused_launch_is_not_pending(world: World, backend: Backend) -> None:
    r = backend.launch("Claude C")
    assert not r.ok and r.message == "already running"
    assert "Claude C" not in backend.pending_launches and world.fake.launched == []


def test_launch_unknown_name(backend: Backend) -> None:
    r = backend.launch("Nope")
    assert not r.ok and r.message == "no launcher named Nope"


def test_launch_all_goes_through_pending_check(world: World, backend: Backend) -> None:
    assert backend.launch("Claude I").ok
    results = dict(backend.launch_all())
    assert results["Claude C"] == ActionResult(False, "already running")
    assert "waiting for it to appear" in results["Claude I"].message
    assert len(world.fake.launched) == 1


def test_launch_all_limited_to_names_keeps_launch_order(world: World, backend: Backend) -> None:
    results = backend.launch_all(["Claude I", "Claude C", "Nope"])
    assert [name for name, _ in results] == ["Claude C", "Claude I"]
    assert world.fake.launched == [world.apps / "Claude I.app"]


# ---- other actions -----------------------------------------------------------------

def test_quit_wraps_quit_instance(world: World, backend: Backend) -> None:
    world.fake.alive.add(501)
    outcome, _ = backend.quit("Claude C")
    assert outcome is QuitOutcome.QUIT and ("send_quit", 501) in world.fake.calls


def test_front_running_and_closed(world: World, backend: Backend) -> None:
    assert backend.front("Claude C").ok and ("bring_to_front", 501) in world.fake.calls
    r = backend.front("Claude I")
    assert not r.ok and r.message == "Claude I is not running"


def test_rearm_opens_launcher_of_running_instance(world: World, backend: Backend) -> None:
    assert backend.rearm("Claude C").ok
    assert world.fake.launched == [world.apps / "Claude C.app"]


def test_move_rewrites_order_file(world: World, backend: Backend) -> None:
    backend.move("Claude P", -1)
    assert (world.cfg / "order.txt").read_text().splitlines() == [
        "Claude C", "Claude P", "Claude I"]


def test_mark_expected_records_current_account(world: World, backend: Backend) -> None:
    r = backend.mark_expected("Claude C")
    assert r.ok and A not in r.message
    assert ExpectedAccounts.load(world.cfg / "expected-accounts.json").get("Claude C") == A
    assert not backend.mark_expected("Claude I").ok


# ---- sign-in support ---------------------------------------------------------------

def test_pending_restore_ignores_live_owner(world: World, backend: Backend) -> None:
    fake = world.fake
    write_restore(world.cfg / "signin-restore.txt", ["Claude C"], owner_pid=4242)
    fake.alive.add(4242)
    assert backend.pending_restore() == []
    fake.alive.discard(4242)
    assert backend.pending_restore() == ["Claude C"]


def test_make_signin_flow(world: World, backend: Backend) -> None:
    flow = backend.make_signin_flow(lambda phase, message: None)
    assert isinstance(flow, SigninFlow)
    assert flow.restore_path == world.cfg / "signin-restore.txt"
    assert flow.log_dir == world.log_dir and flow.refresh == backend.snapshot

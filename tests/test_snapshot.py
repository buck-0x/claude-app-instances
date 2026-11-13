import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import ORG_A, make_data_dir, make_launcher_app

from claude_instances.accounts import ExpectedAccounts
from claude_instances.cookies import SessionState
from claude_instances.launchers import discover_launchers
from claude_instances.processes import ProcessLine
from claude_instances.snapshot import (
    AccountInfo,
    Inputs,
    LiveEntry,
    RejectedMark,
    Snapshot,
    build_snapshot,
)
from claude_instances.usage import LIVE_STALE_AFTER_S, Source, Usage, UsageCache

A = "11111111-2222-4333-8444-555555555555"
B = "22222222-2222-4333-8444-555555555555"
ORG_B = "bbbbbbbb-2222-4333-8444-555555555555"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
EXPIRY = NOW + timedelta(days=30)
TWO_ORG_HISTORY = (Path(__file__).parent / "fixtures/plan-usage-history.json").read_text()


def single_org_history(fh: float, at: datetime, org: str = ORG_A) -> str:
    t = int((at - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() * 1000)
    return json.dumps({"version": 2, "samples": [{"t": t, "org": org, "u": {"fh": fh, "sd": 1}}]})


class World:
    """Three launchers in tmp_path: P (default folder), C and I, plus ps lines.

    P runs argless as 504 and C runs as 501; both are signed in to account A.
    I is closed and signed out, with a single-org history at 44%.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.support = tmp_path / "Support"
        self.claude_app = tmp_path / "FakeClaude.app"
        self.binary = f"{self.claude_app}/Contents/MacOS/Claude"
        self.apps = tmp_path / "Apps"
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
        make_data_dir(self.dirs["Claude I"], None, "none", single_org_history(44, NOW))

    def ps(self, *extra: ProcessLine) -> list[ProcessLine]:
        return [
            ProcessLine(504, self.binary),
            ProcessLine(501, f"{self.binary} --user-data-dir={self.dirs['Claude C']}"),
            ProcessLine(507, f"{self.binary} --type=renderer --user-data-dir={self.dirs['Claude I']}"),
            *extra,
        ]

    def inputs(self, **overrides) -> Inputs:
        inp = Inputs(
            launchers=discover_launchers([self.apps], self.claude_app),
            ps_lines=self.ps(),
            default_data_dir=self.dirs["Claude P"],
            order=["Claude C", "Claude P"],
            expected=ExpectedAccounts(),
            usage_cache=UsageCache(),
            log_dir=self.log_dir,
            now=NOW,
            live={},
            account_info={},
            live_errors={},
            rejected={},
        )
        return replace(inp, **overrides)


def row_of(snap: Snapshot, name: str):
    return snap.row(name)


def test_snapshot_rows_in_order_with_states(tmp_path):
    w = World(tmp_path)
    expected = ExpectedAccounts({"Claude C": B})
    snap = build_snapshot(w.inputs(expected=expected))
    assert [r.name for r in snap.rows] == ["Claude C", "Claude P", "Claude I"]
    assert [r.position for r in snap.rows] == [0, 1, 2]
    assert snap.taken_at == NOW and snap.strays == []
    p = snap.row("Claude P")
    assert p.is_running and p.argless and p.pid == 504 and p.pids == [504]
    assert p.session is SessionState.SIGNED_IN and p.session_expiry == EXPIRY
    assert p.session_created == 1 and p.account == A
    c = snap.row("Claude C")
    assert c.is_running and not c.argless and c.pid == 501
    assert c.shared_with == ["Claude P"] and p.shared_with == ["Claude C"]
    assert c.expected == B and c.expected_mismatch and not p.expected_mismatch
    i = snap.row("Claude I")
    assert not i.is_running and i.pid is None and i.pids == [] and not i.argless
    assert i.session is SessionState.SIGNED_OUT and i.shared_with == []
    assert i.usage.five_hour_pct == 44 and i.usage.source is Source.CLAUDE_HISTORY
    assert not i.watchdog_alive and i.live_error is None


def test_strays_are_mains_matching_no_launcher(tmp_path):
    w = World(tmp_path)
    other = ProcessLine(520, f"{w.binary} --user-data-dir={w.support / 'Elsewhere'}")
    snap = build_snapshot(w.inputs(ps_lines=w.ps(other)))
    assert [s.pid for s in snap.strays] == [520]


def test_live_usage_shown_live_then_downgraded(tmp_path):
    w = World(tmp_path)
    live = Usage(12, NOW + timedelta(hours=1), 30, NOW + timedelta(days=2), NOW, Source.LIVE)
    inp = w.inputs(live={"Claude C": LiveEntry(A, live)})
    assert row_of(build_snapshot(inp), "Claude C").usage == live
    inp.now = NOW + timedelta(seconds=LIVE_STALE_AFTER_S)
    assert row_of(build_snapshot(inp), "Claude C").usage.source is Source.LIVE
    inp.now = NOW + timedelta(seconds=LIVE_STALE_AFTER_S + 1)
    got = row_of(build_snapshot(inp), "Claude C").usage
    assert got == replace(live, source=Source.TUI_CACHE)


def test_live_entry_for_other_account_ignored(tmp_path):
    w = World(tmp_path)
    live = Usage(12, None, 30, None, NOW, Source.LIVE)
    inp = w.inputs(live={"Claude C": LiveEntry(B, live)})
    assert row_of(build_snapshot(inp), "Claude C").usage is None


def test_history_uses_org_of_current_account(tmp_path):
    w = World(tmp_path)
    make_data_dir(w.dirs["Claude C"], A, EXPIRY, TWO_ORG_HISTORY)
    inp = w.inputs(account_info={A: AccountInfo(org=ORG_B)})
    c = row_of(build_snapshot(inp), "Claude C")
    assert (c.usage.five_hour_pct, c.usage.source) == (90, Source.CLAUDE_HISTORY)
    # Without a known org, a two-org history gives nothing.
    assert row_of(build_snapshot(w.inputs()), "Claude C").usage is None


def test_orgless_history_skipped_when_cache_knows_other_account(tmp_path):
    w = World(tmp_path)
    make_data_dir(w.dirs["Claude I"], A, "none", single_org_history(44, NOW))
    cache = UsageCache()
    cache.put("Claude I", B, Usage(70, None, 10, None, NOW - timedelta(days=1), Source.LIVE))
    inp = w.inputs(usage_cache=cache)
    assert row_of(build_snapshot(inp), "Claude I").usage is None
    # A cache entry for the current account keeps the org-less history usable.
    cache.put("Claude I", A, Usage(70, None, 10, None, NOW - timedelta(days=1), Source.LIVE))
    assert row_of(build_snapshot(inp), "Claude I").usage.five_hour_pct == 44


def test_orgless_history_skipped_when_account_unknown_but_cache_has_one(tmp_path):
    w = World(tmp_path)
    cache = UsageCache()
    cache.put("Claude I", B, Usage(70, None, 10, None, NOW, Source.LIVE))
    assert row_of(build_snapshot(w.inputs(usage_cache=cache)), "Claude I").usage is None


def test_email_keyed_by_account(tmp_path):
    w = World(tmp_path)
    inp = w.inputs(account_info={B: AccountInfo(email="b@example.com", org=ORG_B)})
    c = row_of(build_snapshot(inp), "Claude C")
    assert c.email is None and c.org is None
    shown = row_of(build_snapshot(replace(inp, account_info={A: AccountInfo(email="a@example.com", org=ORG_A)})), "Claude C")
    assert shown.email == "a@example.com" and shown.org == ORG_A
    text = repr(shown)
    assert "example.com" not in text and ORG_A not in text and A not in text
    assert "example.com" not in repr(AccountInfo(email="a@example.com", org=ORG_A))


def test_duplicate_flag(tmp_path):
    w = World(tmp_path)
    ps = [ProcessLine(505, f"{w.binary} --user-data-dir={w.dirs['Claude I']}"),
          ProcessLine(506, f"{w.binary} --user-data-dir={w.dirs['Claude I']}")]
    i = row_of(build_snapshot(w.inputs(ps_lines=ps)), "Claude I")
    assert i.is_duplicate and not i.is_running and i.pid is None and i.pids == [505, 506]
    assert not row_of(build_snapshot(w.inputs()), "Claude P").is_duplicate


def test_tui_cache_beats_older_history(tmp_path):
    w = World(tmp_path)
    make_data_dir(w.dirs["Claude C"], A, EXPIRY, single_org_history(5, NOW - timedelta(days=2)))
    cache = UsageCache()
    cache.put("Claude C", A, Usage(60, None, 20, None, NOW - timedelta(hours=1), Source.LIVE))
    c = row_of(build_snapshot(w.inputs(usage_cache=cache)), "Claude C")
    assert (c.usage.five_hour_pct, c.usage.source) == (60, Source.TUI_CACHE)


def test_rejected_cleared_by_new_session(tmp_path):
    w = World(tmp_path)
    inp = w.inputs(rejected={"Claude C": RejectedMark(A, 1)})
    snap = build_snapshot(inp)
    assert row_of(snap, "Claude C").session is SessionState.REJECTED
    assert row_of(snap, "Claude C").shared_with == ["Claude P"]
    make_data_dir(w.dirs["Claude C"], A, EXPIRY, None, created=2)
    assert row_of(build_snapshot(inp), "Claude C").session is SessionState.SIGNED_IN
    make_data_dir(w.dirs["Claude C"], B, EXPIRY, None, created=1)
    assert row_of(build_snapshot(inp), "Claude C").session is SessionState.SIGNED_IN
    # Expired cookies on the refused session read as signed out, not rejected.
    make_data_dir(w.dirs["Claude C"], A, NOW - timedelta(days=1), None, created=1)
    assert row_of(build_snapshot(inp), "Claude C").session is SessionState.SIGNED_OUT


def test_watchdog_alive_in_row(tmp_path):
    w = World(tmp_path)
    launcher = next(l for l in w.inputs().launchers if l.name == "Claude C")
    os.symlink("600", w.log_dir / "Claude C.lock")
    ps = w.ps(ProcessLine(600, f"/bin/zsh {launcher.script_path}"))
    snap = build_snapshot(w.inputs(ps_lines=ps))
    assert row_of(snap, "Claude C").watchdog_alive
    assert not row_of(snap, "Claude P").watchdog_alive
    assert not row_of(build_snapshot(w.inputs()), "Claude C").watchdog_alive


def test_update_wait_in_row_with_blockers(tmp_path):
    w = World(tmp_path)
    launcher = next(l for l in w.inputs().launchers if l.name == "Claude I")
    (w.log_dir / "Claude I.log").write_text(
        "2026-11-06 12:00:00 update pending (staged=101 shipit=none) but not installed:"
        " Claude quit this instance; still running: Claude-C\n")
    stray = ProcessLine(520, f"{w.binary} --user-data-dir={w.support / 'Elsewhere'}")
    # No watchdog: the log alone does not make a waiting row.
    assert not row_of(build_snapshot(w.inputs()), "Claude I").update_waiting
    os.symlink("600", w.log_dir / "Claude I.lock")
    ps = w.ps(ProcessLine(600, f"/bin/zsh {launcher.script_path}"), stray)
    snap = build_snapshot(w.inputs(ps_lines=ps))
    i = row_of(snap, "Claude I")
    assert i.update_waiting
    assert i.update_blockers == ["Claude C", "Claude P", "Elsewhere (no launcher)"]
    assert not row_of(snap, "Claude C").update_waiting


def test_live_error_passed_through(tmp_path):
    w = World(tmp_path)
    snap = build_snapshot(w.inputs(live_errors={"Claude I": "no key"}))
    assert snap.row("Claude I").live_error == "no key" and snap.row("Claude C").live_error is None

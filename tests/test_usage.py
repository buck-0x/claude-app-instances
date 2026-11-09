import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from helpers import ORG_A

from claude_instances.usage import (
    NO_DATA,
    LookupResult,
    Source,
    Usage,
    UsageCache,
    account_hash,
    best_cached,
    format_age,
    format_cell,
    format_until,
    level,
    parse_plan_usage_history,
    read_claude_history,
)

ORG_B = "bbbbbbbb-2222-4333-8444-555555555555"
FIXTURE_PATH = Path(__file__).parent / "fixtures/plan-usage-history.json"
FIXTURE = FIXTURE_PATH.read_text()
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
LIVE_USAGE = Usage(10, NOW + timedelta(hours=1), 20, NOW + timedelta(days=2), NOW, Source.LIVE)


def test_history_latest_for_org():
    u = parse_plan_usage_history(FIXTURE, ORG_A)
    assert (u.five_hour_pct, u.week_pct, u.fetched_at, u.source) == (44, 12, NOW, Source.CLAUDE_HISTORY)
    assert u.five_hour_resets_at is None and u.week_resets_at is None
    b = parse_plan_usage_history(FIXTURE, ORG_B)
    assert (b.five_hour_pct, b.week_pct) == (90, 60)
    assert parse_plan_usage_history(FIXTURE, "cccccccc-2222-4333-8444-555555555555") is None


def test_history_no_org_requires_single_org():
    assert parse_plan_usage_history(FIXTURE, None) is None  # two orgs present
    single = json.dumps({"version": 2, "samples": [{"t": 1793966400000, "org": ORG_A, "u": {"fh": 5, "sd": 6}}]})
    assert parse_plan_usage_history(single, None).five_hour_pct == 5


def test_history_malformed_is_none():
    for text in [
        "",
        "{",
        '{"version": 1, "samples": []}',
        '{"version": 2, "samples": []}',
        '{"version": 2, "samples": [{"t": "x"}]}',
        '[]',
        '{"version": 2, "samples": [{"t": 1, "org": "%s", "u": {"fh": "high", "sd": 1}}]}' % ORG_A,
        '{"version": 2, "samples": [{"t": true, "org": "%s", "u": {"fh": 1, "sd": 1}}]}' % ORG_A,
    ]:
        assert parse_plan_usage_history(text, ORG_A) is None


def test_read_claude_history(tmp_path):
    assert read_claude_history(tmp_path, ORG_A) is None  # no file
    (tmp_path / "plan-usage-history.json").write_text(FIXTURE)
    assert read_claude_history(tmp_path, ORG_A).five_hour_pct == 44
    (tmp_path / "plan-usage-history.json").write_bytes(b"\xff\xfe")
    assert read_claude_history(tmp_path, ORG_A) is None


def test_cache_roundtrip_and_fields_only(tmp_path):
    c = UsageCache()
    c.put("Claude P", ORG_A, Usage(81, NOW + timedelta(minutes=40), 52, NOW + timedelta(days=5), NOW, Source.LIVE))
    c.save(tmp_path / "u.json")
    raw = json.loads((tmp_path / "u.json").read_text())
    assert set(raw["Claude P"]) == {
        "five_hour_pct", "five_hour_resets_at", "week_pct", "week_resets_at", "fetched_at", "account_hash",
    }
    assert ORG_A not in (tmp_path / "u.json").read_text()
    assert stat.S_IMODE((tmp_path / "u.json").stat().st_mode) == 0o600
    loaded = UsageCache.load(tmp_path / "u.json")
    got = loaded.get("Claude P", ORG_A)
    assert got.five_hour_pct == 81 and got.source is Source.TUI_CACHE
    assert got == Usage(81, NOW + timedelta(minutes=40), 52, NOW + timedelta(days=5), NOW, Source.TUI_CACHE)
    assert loaded.stored_hash("Claude P") == account_hash(ORG_A)
    assert loaded.stored_hash("Missing") is None


def test_cache_discarded_on_account_change_and_none(tmp_path):
    c = UsageCache()
    c.put("P", ORG_A, LIVE_USAGE)
    assert c.get("P", ORG_B) is None and c.get("P", None) is None
    c.put("Q", None, LIVE_USAGE)
    assert c.get("Q", None) is None and c.stored_hash("Q") is None


def test_cache_corrupt_file_is_empty(tmp_path):
    path = tmp_path / "u.json"
    path.write_bytes(b"{{{")
    assert UsageCache.load(path).get("P", ORG_A) is None
    assert UsageCache.load(tmp_path / "missing.json").get("P", ORG_A) is None
    path.write_text(json.dumps({"P": {"account_hash": account_hash(ORG_A), "fetched_at": "nope"}}))
    assert UsageCache.load(path).get("P", ORG_A) is None


def test_best_cached_prefers_newer():
    older = Usage(1, None, 2, None, NOW - timedelta(days=1), Source.TUI_CACHE)
    newer = Usage(3, None, 4, None, NOW, Source.CLAUDE_HISTORY)
    assert best_cached(older, newer) is newer
    assert best_cached(newer, older) is newer
    tie = Usage(5, None, 6, None, NOW, Source.TUI_CACHE)
    assert best_cached(tie, newer) is tie
    assert best_cached(None, newer) is newer and best_cached(tie, None) is tie
    assert best_cached(None, None) is None


def test_level_thresholds():
    assert [level(x) for x in (None, 0, 79.9, 80, 94.9, 95, 120)] == ["none", "ok", "ok", "warn", "warn", "crit", "crit"]


def test_format_until_and_age():
    assert format_until(timedelta(hours=2, minutes=10)) == "2h10m" and format_until(timedelta(days=3, hours=5)) == "3d"
    assert format_until(timedelta(minutes=40)) == "0h40m" and format_until(timedelta(seconds=-5)) == "now"
    assert format_age(timedelta(days=37, hours=3)) == "37d" and format_age(timedelta(seconds=30)) == "<1m"
    assert format_age(timedelta(minutes=5, seconds=59)) == "5m" and format_age(timedelta(hours=2, minutes=59)) == "2h"


def test_format_cell():
    live = Usage(12, NOW + timedelta(hours=2, minutes=10), 44, NOW + timedelta(days=3), NOW, Source.LIVE)
    cached = Usage(44, None, None, None, NOW - timedelta(days=37), Source.TUI_CACHE)
    assert format_cell(live.five_hour_pct, live.five_hour_resets_at, live, NOW) == "12%  2h10m"
    assert format_cell(live.five_hour_pct, None, live, NOW) == "12%"
    assert format_cell(cached.five_hour_pct, None, cached, NOW) == "44% · 37d old"
    assert format_cell(None, None, cached, NOW) == NO_DATA
    # A live number older than LIVE_STALE_AFTER_S is shown as cached with its age.
    stale = Usage(12, NOW + timedelta(hours=1), None, None, NOW - timedelta(minutes=7), Source.LIVE)
    assert format_cell(stale.five_hour_pct, stale.five_hour_resets_at, stale, NOW) == "12% · 7m old"


def test_lookup_result_repr_hides_account_details():
    r = LookupResult(usage=None, email="someone@example.com", org=ORG_A)
    assert "example.com" not in repr(r) and ORG_A not in repr(r)
    assert (r.rejected, r.error) == (False, None)

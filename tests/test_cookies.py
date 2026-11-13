import sqlite3
from datetime import datetime, timedelta, timezone

from helpers import chromium_us, make_cookie_db

from claude_instances.cookies import (
    SessionState,
    chromium_time,
    read_claude_cookies,
    session_created,
    session_state,
)

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def test_signed_in_with_future_expiry(tmp_path):
    make_cookie_db(tmp_path / "Cookies", [(".claude.ai", "sessionKey", b"v10xx", chromium_us(NOW + timedelta(days=30)), chromium_us(NOW))])
    store = read_claude_cookies(tmp_path)
    state, exp = session_state(store, NOW)
    assert state is SessionState.SIGNED_IN and exp == NOW + timedelta(days=30)
    assert session_created(store) == chromium_us(NOW)


def test_session_cookie_zero_expiry_is_signed_in(tmp_path):
    make_cookie_db(tmp_path / "Cookies", [(".claude.ai", "sessionKey", b"v10xx", 0, chromium_us(NOW))])
    assert session_state(read_claude_cookies(tmp_path), NOW) == (SessionState.SIGNED_IN, None)


def test_expired_is_signed_out(tmp_path):
    make_cookie_db(tmp_path / "Cookies", [(".claude.ai", "sessionKey", b"v10xx", chromium_us(NOW - timedelta(days=1)), chromium_us(NOW - timedelta(days=31)))])
    assert session_state(read_claude_cookies(tmp_path), NOW) == (SessionState.SIGNED_OUT, None)


def test_no_session_cookie_is_signed_out(tmp_path):
    make_cookie_db(tmp_path / "Cookies", [(".claude.ai", "lastActiveOrg", b"v10org", 0, chromium_us(NOW))])
    store = read_claude_cookies(tmp_path)
    assert store is not None and [r.name for r in store.rows] == ["lastActiveOrg"]
    assert session_state(store, NOW) == (SessionState.SIGNED_OUT, None)
    assert session_created(store) is None


def test_other_host_ignored(tmp_path):
    make_cookie_db(tmp_path / "Cookies", [(".example.com", "sessionKey", b"v10xx", 0, chromium_us(NOW))])
    store = read_claude_cookies(tmp_path)
    assert store is not None and store.rows == []
    assert session_state(store, NOW) == (SessionState.SIGNED_OUT, None)


def test_missing_db_is_unknown(tmp_path):
    assert read_claude_cookies(tmp_path) is None
    assert session_state(None, NOW) == (SessionState.UNKNOWN, None)
    assert session_created(None) is None


def test_network_fallback(tmp_path):
    make_cookie_db(tmp_path / "Network" / "Cookies", [(".claude.ai", "sessionKey", b"v10xx", 0, 7)])
    store = read_claude_cookies(tmp_path)
    assert store is not None and session_created(store) == 7


def test_path_with_space_and_hash(tmp_path):
    d = tmp_path / "Application Support" / "#x"
    d.mkdir(parents=True)
    make_cookie_db(d / "Cookies", [(".claude.ai", "sessionKey", b"v10xx", 0, 1)])
    assert read_claude_cookies(d) is not None


def test_database_error_retries_then_unknown(tmp_path):
    (tmp_path / "Cookies").write_bytes(b"not a database")
    calls = []
    assert read_claude_cookies(tmp_path, retries=3, sleep=calls.append) is None
    assert len(calls) == 2


def test_chromium_time():
    assert chromium_time(0) is None
    assert chromium_time(11644473600_000_000) == datetime(1970, 1, 1, tzinfo=timezone.utc)


def test_db_version_read(tmp_path):
    make_cookie_db(tmp_path / "Cookies", [(".claude.ai", "sessionKey", b"v10xx", 0, 1)], version=24)
    store = read_claude_cookies(tmp_path)
    assert store is not None and store.db_version == 24


def test_host_match_is_dot_bounded(tmp_path):
    make_cookie_db(tmp_path / "Cookies", [
        ("claude.ai", "sessionKey", b"v10a", 0, 1),
        (".claude.ai", "sessionKey", b"v10b", 0, 2),
        ("www.claude.ai", "sessionKey", b"v10c", 0, 3),
        ("evilclaude.ai", "sessionKey", b"v10d", 0, 4),
        (".notclaude.ai", "sessionKey", b"v10e", 0, 5),
    ])
    store = read_claude_cookies(tmp_path)
    assert sorted(r.host_key for r in store.rows) == [".claude.ai", "claude.ai", "www.claude.ai"]


def test_bad_meta_version_is_unknown_not_raised(tmp_path):
    make_cookie_db(tmp_path / "Cookies", [(".claude.ai", "sessionKey", b"v10xx", 0, 1)])
    con = sqlite3.connect(tmp_path / "Cookies")
    con.execute("UPDATE meta SET value = 'x' WHERE key = 'version'")
    con.commit()
    con.close()
    slept = []
    assert read_claude_cookies(tmp_path, sleep=slept.append) is None
    assert slept == []  # a value that will not change is not retried


def test_out_of_range_times_do_not_raise(tmp_path):
    huge = 2**62
    make_cookie_db(tmp_path / "Cookies", [(".claude.ai", "sessionKey", b"v10xx", huge, 1)])
    slept = []
    store = read_claude_cookies(tmp_path, sleep=slept.append)
    assert slept == []
    # A row whose expiry is past datetime's range is unusable: never raised.
    assert session_state(store, NOW) == (SessionState.UNKNOWN, None)

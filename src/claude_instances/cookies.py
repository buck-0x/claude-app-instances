"""Read-only access to a Claude data dir's Cookies DB and the signed-in state it implies.

The DB is opened through a percent-encoded `file:...?immutable=1` URI so a running
Claude is never disturbed and nothing is written. Cookie values stay encrypted here;
decryption is a separate concern.
"""

import sqlite3
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

_CHROMIUM_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)
_RETRY_DELAY_S = 0.2

_QUERY = (
    "SELECT name, host_key, encrypted_value, expires_utc, creation_utc FROM cookies"
    " WHERE (host_key IN ('claude.ai', '.claude.ai') OR host_key LIKE '%.claude.ai')"
    " AND name IN ('sessionKey', 'lastActiveOrg')"
)


@dataclass
class CookieRow:
    name: str
    host_key: str
    encrypted_value: bytes = field(repr=False)
    expires_utc: int
    creation_utc: int


@dataclass
class CookieStore:
    rows: list[CookieRow]
    db_version: int


class SessionState(Enum):
    SIGNED_IN = "signed_in"
    SIGNED_OUT = "signed_out"
    UNKNOWN = "unknown"
    REJECTED = "rejected"


class _UnusableValue(Exception):
    """The DB was read but holds a value that a retry would read again unchanged."""


def chromium_time(us: int) -> datetime | None:
    """Convert Chromium microseconds-since-1601 to an aware UTC datetime; 0 means none."""
    if us == 0:
        return None
    return _CHROMIUM_EPOCH + timedelta(microseconds=us)


def _cookies_path(data_dir: Path) -> Path | None:
    for candidate in (data_dir / "Cookies", data_dir / "Network" / "Cookies"):
        if candidate.is_file():
            return candidate
    return None


def _read(path: Path) -> CookieStore:
    uri = "file:" + urllib.parse.quote(str(path)) + "?immutable=1"
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = [CookieRow(*r) for r in con.execute(_QUERY)]
        meta = con.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        if meta is None:
            raise sqlite3.DatabaseError("Cookies DB has no meta version")
        try:
            version = int(meta[0])
            # A time datetime cannot hold would raise later, in every snapshot.
            for row in rows:
                chromium_time(row.expires_utc)
                chromium_time(row.creation_utc)
        except (ValueError, TypeError, OverflowError):
            raise _UnusableValue() from None
        return CookieStore(rows=rows, db_version=version)
    finally:
        con.close()


def read_claude_cookies(
    data_dir: Path, retries: int = 3, sleep=time.sleep
) -> CookieStore | None:
    """Read claude.ai session cookies; None if there is no DB or every attempt failed."""
    path = _cookies_path(data_dir)
    if path is None:
        return None
    for attempt in range(retries):
        if attempt:
            sleep(_RETRY_DELAY_S)
        try:
            return _read(path)
        except sqlite3.DatabaseError:
            continue  # often a write in progress: worth another try
        except _UnusableValue:
            return None
    return None


def _session_rows(store: CookieStore) -> list[CookieRow]:
    return [r for r in store.rows if r.name == "sessionKey"]


def session_state(
    store: CookieStore | None, now: datetime
) -> tuple[SessionState, datetime | None]:
    """Signed-in state from the cookies alone, with the session expiry when signed in.

    A zero `expires_utc` is a browser-session cookie: signed in with no known expiry.
    REJECTED is never produced here; it comes from a server refusing the session.
    """
    if store is None:
        return SessionState.UNKNOWN, None
    expiries = [chromium_time(r.expires_utc) for r in _session_rows(store)]
    live = [e for e in expiries if e is None or e > now]
    if not live:
        return SessionState.SIGNED_OUT, None
    if None in live:
        return SessionState.SIGNED_IN, None
    return SessionState.SIGNED_IN, max(live)


def session_created(store: CookieStore | None) -> int | None:
    """Largest `creation_utc` among sessionKey rows, to notice a fresh sign-in."""
    if store is None:
        return None
    return max((r.creation_utc for r in _session_rows(store)), default=None)

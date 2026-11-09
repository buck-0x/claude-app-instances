"""Shared test fixture builders. Everything here writes under a caller-given tmp path."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_CHROMIUM_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def chromium_us(dt: datetime) -> int:
    """Microseconds since 1601-01-01 UTC: the inverse of `cookies.chromium_time`."""
    delta = dt - _CHROMIUM_EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def make_cookie_db(
    path: Path, rows: list[tuple[str, str, bytes, int, int]], version: int = 24
) -> None:
    """Create a minimal Chromium-shaped Cookies DB.

    Each row is (host_key, name, encrypted_value, expires_utc, creation_utc).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute(
            "CREATE TABLE cookies(host_key TEXT, name TEXT, encrypted_value BLOB,"
            " expires_utc INTEGER, creation_utc INTEGER)"
        )
        con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        con.executemany("INSERT INTO cookies VALUES (?, ?, ?, ?, ?)", rows)
        con.execute("INSERT INTO meta VALUES ('version', ?)", (str(version),))
        con.commit()
    finally:
        con.close()

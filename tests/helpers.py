"""Shared test fixture builders. Everything here writes under a caller-given tmp path."""

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from claude_instances.crypto import derive_key
from claude_instances.secret import Secret

_CHROMIUM_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

ORG_A = "aaaaaaaa-2222-4333-8444-555555555555"


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


def encrypt_cookie(plaintext: str, key: bytes, host_key: str, db_version: int) -> bytes:
    """Encrypt like macOS Chromium: the inverse of `crypto.decrypt_cookie`."""
    raw = plaintext.encode("utf-8")
    if db_version >= 24:
        raw = hashlib.sha256(host_key.encode("utf-8")).digest() + raw
    padder = padding.PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return b"v10" + enc.update(padded) + enc.finalize()


def make_signed_in_dir(
    root: Path,
    password: Secret,
    org: str | None = ORG_A,
    session: str = "sk-ant-fake-session",
    created: int = 1,
) -> Path:
    """A data dir whose Cookies DB holds an unexpired encrypted sessionKey (and org)."""
    key = derive_key(password).reveal()
    expires = chromium_us(datetime(2099, 1, 1, tzinfo=timezone.utc))
    rows = [(".claude.ai", "sessionKey", encrypt_cookie(session, key, ".claude.ai", 24),
             expires, created)]
    if org is not None:
        rows.append((".claude.ai", "lastActiveOrg",
                     encrypt_cookie(org, key, ".claude.ai", 24), expires, created))
    make_cookie_db(root / "Cookies", rows, version=24)
    return root

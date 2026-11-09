"""Shared test fixture builders. Everything here writes under a caller-given tmp path."""

import hashlib
import json
import os
import plistlib
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from claude_instances.crypto import derive_key
from claude_instances.httpclient import ACCOUNT_PATH, ORGS_PATH, HttpResult
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


class FakeHttp:
    """HttpClient double: records request paths and answers per endpoint.

    Returns `usage` for paths ending `/usage`, `account` for ACCOUNT_PATH, `orgs` for
    ORGS_PATH, or a 404 "other" result when the matching argument is None.
    """

    def __init__(
        self,
        orgs: HttpResult | None = None,
        account: HttpResult | None = None,
        usage: HttpResult | None = None,
    ) -> None:
        self.orgs = orgs
        self.account = account
        self.usage = usage
        self.paths: list[str] = []
        self.cookies: list[dict[str, Secret]] = []

    def get(self, url: str, cookies: dict[str, Secret]) -> HttpResult:
        path = urllib.parse.urlsplit(url).path
        self.paths.append(path)
        self.cookies.append(cookies)
        if path.endswith("/usage"):
            result = self.usage
        elif path == ACCOUNT_PATH:
            result = self.account
        elif path == ORGS_PATH:
            result = self.orgs
        else:
            result = None
        return result if result is not None else HttpResult(404, None, "other")


def pipe_with(data: bytes) -> int:
    """Read end of a pipe holding `data`, with the write end already closed."""
    r, w = os.pipe()
    try:
        os.write(w, data)
    finally:
        os.close(w)
    return r


def make_launcher_app(
    apps_dir: Path,
    name: str,
    data_dir: Path,
    bundle_id: str | None = None,
    fmt: Literal["argless", "watchdog", "oneshot"] = "argless",
    claude_app: Path = Path("/Applications/Claude.app"),
    default_data_dir: Path | None = None,
) -> Path:
    """Write `<apps_dir>/<name>.app` shaped like a `new-claude` launcher; return its path.

    `argless` and `watchdog` mirror the generated header (argless adds the
    DEFAULT_DATA_DIR line); `oneshot` is the old single inline `open` line.
    """
    app = apps_dir / f"{name}.app"
    (app / "Contents/MacOS").mkdir(parents=True, exist_ok=True)
    if bundle_id is None:
        bundle_id = f"local.launcher.{data_dir.name.lower()}"
    with open(app / "Contents/Info.plist", "wb") as f:
        plistlib.dump({
            "CFBundleName": name,
            "CFBundleDisplayName": name,
            "CFBundleIdentifier": bundle_id,
            "CFBundleExecutable": "launcher",
            "CFBundlePackageType": "APPL",
        }, f)
    if fmt == "oneshot":
        body = f'exec open -n -a "{claude_app}" --args --user-data-dir="{data_dir}"\n'
    else:
        body = f'CLAUDE_APP="{claude_app}"\nDATA_DIR="{data_dir}"\n'
        if fmt == "argless":
            default = default_data_dir or data_dir.parent / "Claude"
            body += f'DEFAULT_DATA_DIR="{default}"\n'
        body += f'INSTANCE_NAME="{name}"\n\n'
        body += 'open -n -a "$CLAUDE_APP" --args --user-data-dir="$DATA_DIR"\n'
    (app / "Contents/MacOS/launcher").write_text("#!/bin/zsh\n" + body)
    return app


def make_data_dir(
    root: Path,
    account: str | None,
    session_expiry: datetime | None | Literal["none"],
    history: str | None,
    created: int = 1,
) -> Path:
    """A data dir with `config.json`, a `Cookies` DB and optionally plan usage history.

    `session_expiry` is the sessionKey expiry: a datetime, None for a browser-session
    cookie (expires_utc 0), or "none" for no sessionKey row at all. The cookie value is
    a placeholder; nothing here decrypts it. `account` None leaves config.json empty.
    """
    root.mkdir(parents=True, exist_ok=True)
    config = {} if account is None else {"lastKnownAccountUuid": account}
    (root / "config.json").write_text(json.dumps(config))
    rows = []
    if session_expiry != "none":
        expires = 0 if session_expiry is None else chromium_us(session_expiry)
        rows.append((".claude.ai", "sessionKey", b"v10placeholder", expires, created))
    cookies = root / "Cookies"
    cookies.unlink(missing_ok=True)
    make_cookie_db(cookies, rows, version=24)
    if history is not None:
        (root / "plan-usage-history.json").write_text(history)
    return root

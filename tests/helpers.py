"""Shared test fixture builders. Everything here writes under a caller-given tmp path."""

import hashlib
import json
import os
import plistlib
import signal
import sqlite3
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from claude_instances.actions import ActionResult
from claude_instances.crypto import derive_key
from claude_instances.httpclient import ACCOUNT_PATH, ORGS_PATH, HttpResult
from claude_instances.processes import ProcessLine
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


class FakeSystemActions:
    """SystemActions double: records every call and never touches the real machine.

    Every method first calls `before_call(method_name)` when set (raise there to
    simulate a failure), then appends `(method_name, *args)` to `calls`. `send_quit`
    and any non-SIGKILL `signal_pids` make a pid vanish from `alive` and `ps` on the
    `quit_after`-th later `processes()`/`is_alive()` poll (at once when 0); SIGKILL
    removes it at once. Pids in `never_dies` never vanish; pids in `sigterm_ignored`
    survive SIGTERM (a watchdog in a foreground sleep) but not SIGKILL. A pid in
    `quit_results` gets that result from `send_quit` and does not vanish. `launch`
    records the target in `launched` and calls `on_launch(app_path)` when set.
    `locks` maps watchdog lock paths to the pid they name, in memory only.
    """

    def __init__(
        self,
        ps: list[ProcessLine] | None = None,
        alive: set[int] | None = None,
        quit_after: int = 1,
        never_dies: set[int] | None = None,
        front_result: ActionResult | None = None,
        on_launch: Callable[[Path], None] | None = None,
        before_call: Callable[[str], None] | None = None,
        sigterm_ignored: set[int] | None = None,
        quit_results: dict[int, ActionResult] | None = None,
        locks: dict[Path, int] | None = None,
    ) -> None:
        self.ps = list(ps or [])
        self.alive = set(alive or ())
        self.quit_after = quit_after
        self.never_dies = set(never_dies or ())
        self.front_result = front_result
        self.on_launch = on_launch
        self.before_call = before_call
        self.sigterm_ignored = set(sigterm_ignored or ())
        self.quit_results = dict(quit_results or {})
        self.locks = dict(locks or {})
        self.calls: list[tuple] = []
        self.launched: list[Path] = []
        self._dying: dict[int, int] = {}  # pid -> polls left before it vanishes

    def _record(self, name: str, *args: object) -> None:
        if self.before_call is not None:
            self.before_call(name)
        self.calls.append((name, *args))

    def _remove(self, pid: int) -> None:
        self._dying.pop(pid, None)
        self.alive.discard(pid)
        self.ps = [line for line in self.ps if line.pid != pid]

    def _doom(self, pid: int) -> None:
        if pid in self.never_dies:
            return
        if self.quit_after <= 0:
            self._remove(pid)
        else:
            self._dying.setdefault(pid, self.quit_after)

    def _tick(self) -> None:
        for pid in list(self._dying):
            self._dying[pid] -= 1
            if self._dying[pid] <= 0:
                self._remove(pid)

    def bring_to_front(self, pid: int) -> ActionResult:
        self._record("bring_to_front", pid)
        return self.front_result or ActionResult(True, "brought to front")

    def launch(self, app_path: Path) -> ActionResult:
        self._record("launch", app_path)
        self.launched.append(app_path)
        if self.on_launch is not None:
            self.on_launch(app_path)
        return ActionResult(True, "launched")

    def send_quit(self, pid: int) -> ActionResult:
        self._record("send_quit", pid)
        if pid in self.quit_results:
            return self.quit_results[pid]
        self._doom(pid)
        return ActionResult(True, "asked to quit")

    def signal_pids(self, pids: list[int], sig: int) -> None:
        self._record("signal_pids", list(pids), sig)
        for pid in pids:
            if sig == signal.SIGKILL:
                if pid not in self.never_dies:
                    self._remove(pid)
            elif pid not in self.sigterm_ignored:
                self._doom(pid)

    def is_alive(self, pid: int) -> bool:
        self._record("is_alive", pid)
        self._tick()
        return pid in self.alive

    def processes(self) -> list[ProcessLine]:
        self._record("processes")
        self._tick()
        return list(self.ps)

    def remove_lock_if_names(self, lock: Path, pid: int) -> bool:
        self._record("remove_lock_if_names", lock, pid)
        if self.locks.get(lock) != pid:
            return False
        del self.locks[lock]
        return True


class ScriptedRefresh:
    """A snapshot `refresh` double that replays `items` in order.

    The n-th call (1-based, counted in `calls`) first runs `on_call.get(n)` when set,
    then raises the item if it is an exception or returns it; the last item repeats
    once the list is exhausted. Returned snapshots are recorded in `returned`.
    """

    def __init__(
        self,
        items: list[object],
        on_call: dict[int, Callable[[], None]] | None = None,
    ) -> None:
        if not items:
            raise ValueError("ScriptedRefresh needs at least one item")
        self.items = list(items)
        self.on_call = dict(on_call or {})
        self.calls = 0
        self.returned: list[object] = []

    def __call__(self) -> object:
        self.calls += 1
        hook = self.on_call.get(self.calls)
        if hook is not None:
            hook()
        item = self.items[min(self.calls, len(self.items)) - 1]
        if isinstance(item, BaseException):
            raise item
        self.returned.append(item)
        return item

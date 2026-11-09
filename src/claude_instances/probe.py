"""One-shot probe of the claude.ai endpoints that prints response shapes, never values.

Used at a user-run checkpoint to confirm the endpoint paths and the usage payload's
structure. Output holds only paths, statuses, kinds and a shape: field names, types
and coarse numeric buckets. Ids used as dict keys are redacted too.
"""

import json
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from claude_instances.cookies import read_claude_cookies
from claude_instances.crypto import decrypt_store, derive_key
from claude_instances.httpclient import (
    ACCOUNT_PATH,
    BASE_URL,
    ORGS_PATH,
    USAGE_PATH,
    HttpClient,
    HttpResult,
)
from claude_instances.secret import read_secret_fd

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_DIGITS_KEY = re.compile(r"^\d{6,}$")


def _bucket(n: int | float) -> str:
    if n < 0:
        return "<0"
    if n == 0:
        return "0"
    if n <= 1:
        return "(0,1]"
    if n <= 100:
        return "(1,100]"
    if n <= 1e9:
        return "(100,1e9]"
    return ">1e9"


def _is_iso_datetime(s: str) -> bool:
    if "-" not in s and ":" not in s:
        return False
    try:
        datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _shape_str(s: str) -> str:
    if _is_iso_datetime(s):
        return "str:iso-datetime"
    if _UUID.match(s):
        return "str:uuid-like"
    if "@" in s:
        return "str:email-like"
    return f"str:len={len(s)}"


def redact_key(k: str) -> str:
    if _UUID.match(k):
        return "<uuid-key>"
    if "@" in k:
        return "<email-key>"
    if _DIGITS_KEY.match(k):
        return "<digits-key>"
    return k


def shape(value: Any) -> Any:
    """Structure of a JSON value with every leaf replaced by a type/bucket label."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return f"int:{_bucket(value)}"
    if isinstance(value, float):
        return f"float:{_bucket(value)}"
    if isinstance(value, str):
        return _shape_str(value)
    if isinstance(value, list):
        return {"list": shape(value[0]) if value else None, "len": len(value)}
    if isinstance(value, dict):
        return {redact_key(str(k)): shape(v) for k, v in value.items()}
    return type(value).__name__


def _first_org(orgs: HttpResult) -> str | None:
    if isinstance(orgs.json, list):
        for org in orgs.json:
            if isinstance(org, dict) and isinstance(org.get("uuid"), str):
                return org["uuid"]
    return None


def _report(path_template: str, result: HttpResult, out: Callable[[str], Any]) -> None:
    line = f"GET {path_template} -> {result.status} {result.kind}"
    out(line + (f" {result.error}" if result.error else ""))
    if result.json is not None:
        out(json.dumps(shape(result.json), indent=1))


def run_probe(
    data_dir: Path, key_fd: int, http: HttpClient, out: Callable[[str], Any] = print
) -> int:
    """GET orgs, account and usage with the dir's cookies and print each shape.

    Returns 0 once the requests were made (whatever their status); on a missing or
    unreadable key, missing cookies or a decryption failure prints one line with no
    secret and returns 1.
    """
    try:
        password = read_secret_fd(key_fd)
    except ValueError as e:
        out(f"probe: {e}")
        return 1
    if password is None:
        out(f"probe: no key on fd {key_fd}")
        return 1
    store = read_claude_cookies(data_dir)
    if store is None:
        out("probe: no readable Cookies DB in the data dir")
        return 1
    try:
        cookies = decrypt_store(store, derive_key(password))
    except ValueError as e:
        out(f"probe: cannot decrypt cookies: {e}")
        return 1
    if "sessionKey" not in cookies:
        out("probe: no sessionKey cookie in the data dir")
        return 1

    orgs = http.get(BASE_URL + ORGS_PATH, cookies)
    _report(ORGS_PATH, orgs, out)
    _report(ACCOUNT_PATH, http.get(BASE_URL + ACCOUNT_PATH, cookies), out)

    last = cookies.get("lastActiveOrg")
    org = last.reveal() if last is not None else _first_org(orgs)
    if not org:
        out(f"GET {USAGE_PATH} -> skipped: no org")
        return 0
    usage_url = BASE_URL + USAGE_PATH.format(org=urllib.parse.quote(org, safe=""))
    _report(USAGE_PATH, http.get(usage_url, cookies), out)
    return 0

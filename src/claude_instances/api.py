"""Live usage lookup against the claude.ai web API, shaped by the Checkpoint A probe.

The usage response holds `five_hour` and `seven_day` windows, each null or an object
with `utilization` (percent 0..100) and `resets_at` (ISO datetime). The account
response carries `email_address`; the organizations response is a list of org
objects with `uuid`. `UsageLookup.lookup` never raises, and its error strings are
fixed text plus a status or exception type name: never URLs, ids, headers, cookie
values or body text.
"""

import math
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
from claude_instances.secret import Secret
from claude_instances.usage import Lookup, LookupResult, Source, Usage

_FIVE_HOUR = "five_hour"
_WEEK = "seven_day"


def _pct(window: dict) -> float | None:
    value = window.get("utilization")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def _reset(window: dict) -> datetime | None:
    value = window.get("resets_at")
    if not isinstance(value, str):
        return None
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _window(data: dict, key: str) -> tuple[float | None, datetime | None]:
    window = data.get(key)
    if not isinstance(window, dict):
        return None, None
    return _pct(window), _reset(window)


def parse_usage_response(data: Any, fetched_at: datetime) -> Usage | None:
    """Usage from a usage response, or None when neither window key is present.

    A null or malformed window, a non-number utilization or a bad date gives None
    for that field only.
    """
    if not isinstance(data, dict) or (_FIVE_HOUR not in data and _WEEK not in data):
        return None
    five_pct, five_reset = _window(data, _FIVE_HOUR)
    week_pct, week_reset = _window(data, _WEEK)
    return Usage(five_pct, five_reset, week_pct, week_reset, fetched_at, Source.LIVE)


def parse_account_email(data: Any) -> str | None:
    """The account's `email_address` when it looks like an email, else None."""
    if not isinstance(data, dict):
        return None
    email = data.get("email_address")
    return email if isinstance(email, str) and "@" in email else None


def parse_org_uuid(data: Any) -> str | None:
    """The first org's `uuid` from an organizations response, else None."""
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    uuid = data[0].get("uuid")
    return uuid if isinstance(uuid, str) and uuid else None


def _failure(result: HttpResult) -> LookupResult | None:
    """The outcome for a non-200 (or transport-failed) response; None for a 200."""
    if result.kind == "error":
        return LookupResult(None, error=f"live unavailable ({result.error})")
    # Only a 401 means the session was refused. A 403 can mean the account may not
    # read this org's usage while its session works.
    if result.status == 401:
        return LookupResult(None, rejected=True)
    if result.status != 200:
        return LookupResult(None, error=f"live unavailable ({result.status})")
    return None


class UsageLookup(Lookup):
    """Look up one data dir's usage with its own decrypted claude.ai cookies.

    `key` is the Safe Storage password; the AES key is derived from it here.
    """

    def __init__(
        self,
        http: HttpClient,
        key: Secret,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._http = http
        self._key = key
        self._now = now

    def lookup(self, data_dir: Path) -> LookupResult:
        try:
            return self._lookup(data_dir)
        except Exception as e:  # noqa: BLE001: report the type only, never the message
            return LookupResult(None, error=f"lookup failed ({type(e).__name__})")

    def _lookup(self, data_dir: Path) -> LookupResult:
        store = read_claude_cookies(data_dir)
        if store is None:
            return LookupResult(None, error="no session cookie")
        try:
            cookies = decrypt_store(store, derive_key(self._key))
        except ValueError:
            return LookupResult(None, error="cannot decrypt cookies (wrong key?)")
        if "sessionKey" not in cookies:
            return LookupResult(None, error="no session cookie")

        last = cookies.get("lastActiveOrg")
        org = last.reveal() if last is not None else None
        if not org:
            orgs = self._http.get(BASE_URL + ORGS_PATH, cookies)
            failed = _failure(orgs)
            if failed is not None:
                return failed
            org = parse_org_uuid(orgs.json)
            if org is None:
                return LookupResult(None, error="no organization")

        usage_url = BASE_URL + USAGE_PATH.format(org=urllib.parse.quote(org, safe=""))
        result = self._http.get(usage_url, cookies)
        failed = _failure(result)
        if failed is not None:
            return failed
        usage = parse_usage_response(result.json, self._now()) if result.kind == "json" else None
        if usage is None:
            return LookupResult(None, error="unexpected usage response")

        account = self._http.get(BASE_URL + ACCOUNT_PATH, cookies)
        email = parse_account_email(account.json) if account.status == 200 else None
        return LookupResult(usage, email=email, org=org)

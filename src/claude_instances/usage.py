"""Usage numbers, where they come from, the TUI's own cache and how they are shown.

Usage comes from a live lookup (Task 10), from the TUI's cache of earlier live
lookups, or from Claude's own `plan-usage-history.json` in the instance's data dir.
Every number carries its `fetched_at`; a number counts as live only while it is
from a live lookup no older than `LIVE_STALE_AFTER_S`, otherwise it is shown with
its age.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal, Protocol

from claude_instances.jsonfile import write_private_json

WARN_PCT = 80
CRIT_PCT = 95
LIVE_INTERVAL_S = 300
LIVE_STALE_AFTER_S = 360
NO_DATA = "\N{EM DASH}"

HISTORY_FILENAME = "plan-usage-history.json"
HISTORY_VERSION = 2
ACCOUNT_HASH_LEN = 12

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_TIME_FIELDS = ("five_hour_resets_at", "week_resets_at", "fetched_at")
_PCT_FIELDS = ("five_hour_pct", "week_pct")


class Source(Enum):
    LIVE = "live"
    TUI_CACHE = "tui_cache"
    CLAUDE_HISTORY = "claude_history"


@dataclass(frozen=True)
class Usage:
    five_hour_pct: float | None
    five_hour_resets_at: datetime | None
    week_pct: float | None
    week_resets_at: datetime | None
    fetched_at: datetime
    source: Source


@dataclass
class LookupResult:
    """Outcome of one live lookup. Account details are kept out of the repr."""

    usage: Usage | None
    rejected: bool = False
    error: str | None = None
    email: str | None = field(default=None, repr=False)
    org: str | None = field(default=None, repr=False)


class Lookup(Protocol):
    def lookup(self, data_dir: Path) -> LookupResult:
        """Look up one instance's usage. Must never raise."""
        ...


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_sample(sample: Any) -> tuple[int | float, str, float | None, float | None] | None:
    """(t, org, fh, sd) from one history sample, or None if it is malformed."""
    if not isinstance(sample, dict):
        return None
    t, org, u = sample.get("t"), sample.get("org"), sample.get("u")
    if not _is_number(t) or not isinstance(org, str) or not isinstance(u, dict):
        return None
    fh, sd = u.get("fh"), u.get("sd")
    if not all(v is None or _is_number(v) for v in (fh, sd)):
        return None
    return t, org, fh, sd


def parse_plan_usage_history(text: str, org: str | None) -> Usage | None:
    """The latest sample for `org` from Claude's history file, or None.

    With `org` None, a sample is used only when every sample has the same org, so
    an account switch never shows another org's numbers. Any malformation gives None.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("version") != HISTORY_VERSION:
        return None
    raw_samples = data.get("samples")
    if not isinstance(raw_samples, list) or not raw_samples:
        return None
    samples = [_parse_sample(s) for s in raw_samples]
    if any(s is None for s in samples):
        return None
    if org is None:
        if len({s[1] for s in samples}) != 1:
            return None
    else:
        samples = [s for s in samples if s[1] == org]
        if not samples:
            return None
    t, _, fh, sd = max(samples, key=lambda s: s[0])
    try:
        fetched_at = _EPOCH + timedelta(milliseconds=t)
    except OverflowError:
        return None
    return Usage(fh, None, sd, None, fetched_at, Source.CLAUDE_HISTORY)


def read_claude_history(data_dir: Path, org: str | None) -> Usage | None:
    """Read `<data_dir>/plan-usage-history.json` (never written back) and parse it."""
    try:
        text = (data_dir / HISTORY_FILENAME).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return parse_plan_usage_history(text, org)


def account_hash(uuid: str) -> str:
    """A short SHA-256 prefix of an account UUID, stored instead of the UUID."""
    return hashlib.sha256(uuid.encode("utf-8")).hexdigest()[:ACCOUNT_HASH_LEN]


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("timestamp has no time zone")
    return dt


class UsageCache:
    """Display name to the last live usage numbers, tagged with an account hash.

    Only numbers, times and the hash are stored: no emails, ids or raw responses.
    """

    def __init__(self, entries: dict[str, dict[str, Any]] | None = None) -> None:
        self._entries: dict[str, dict[str, Any]] = dict(entries or {})

    @classmethod
    def load(cls, path: Path) -> "UsageCache":
        """Read the cache; a missing or corrupt file gives an empty cache."""
        try:
            data = json.loads(path.read_bytes())
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        return cls({k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)})

    def save(self, path: Path) -> None:
        """Write the cache atomically with mode 0600."""
        write_private_json(path, self._entries)

    def get(self, name: str, account_uuid: str | None) -> Usage | None:
        """The cached usage for `name` if it was fetched for `account_uuid`, else None."""
        if account_uuid is None:
            return None
        entry = self._entries.get(name)
        if entry is None or entry.get("account_hash") != account_hash(account_uuid):
            return None
        try:
            pcts = [entry.get(k) for k in _PCT_FIELDS]
            if not all(p is None or _is_number(p) for p in pcts):
                return None
            five_resets = entry.get("five_hour_resets_at")
            week_resets = entry.get("week_resets_at")
            return Usage(
                five_hour_pct=pcts[0],
                five_hour_resets_at=None if five_resets is None else _parse_time(five_resets),
                week_pct=pcts[1],
                week_resets_at=None if week_resets is None else _parse_time(week_resets),
                fetched_at=_parse_time(entry.get("fetched_at")),
                source=Source.TUI_CACHE,
            )
        except ValueError:
            return None

    def put(self, name: str, account_uuid: str | None, usage: Usage) -> None:
        """Store `usage` for `name`; without a known account nothing is stored."""
        if account_uuid is None:
            return

        def iso(dt: datetime | None) -> str | None:
            return None if dt is None else dt.isoformat()

        self._entries[name] = {
            "five_hour_pct": usage.five_hour_pct,
            "five_hour_resets_at": iso(usage.five_hour_resets_at),
            "week_pct": usage.week_pct,
            "week_resets_at": iso(usage.week_resets_at),
            "fetched_at": iso(usage.fetched_at),
            "account_hash": account_hash(account_uuid),
        }

    def stored_hash(self, name: str) -> str | None:
        """The account hash saved with `name`'s entry, or None if there is none."""
        entry = self._entries.get(name)
        value = entry.get("account_hash") if entry is not None else None
        return value if isinstance(value, str) else None


def best_cached(tui: Usage | None, claude: Usage | None) -> Usage | None:
    """The newer of the two by `fetched_at`; the TUI cache wins ties."""
    if tui is None or claude is None:
        return tui or claude
    return claude if claude.fetched_at > tui.fetched_at else tui


def level(pct: float | None) -> Literal["ok", "warn", "crit", "none"]:
    if pct is None:
        return "none"
    if pct >= CRIT_PCT:
        return "crit"
    if pct >= WARN_PCT:
        return "warn"
    return "ok"


def format_age(delta: timedelta) -> str:
    """How old something is: "<1m", "5m", "2h" or "37d" (rounded down)."""
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "<1m"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86_400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86_400}d"


def format_until(delta: timedelta) -> str:
    """Time until a reset: "2h10m" below one day, "3d" at or above, "now" if past."""
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "now"
    if seconds < 86_400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 86_400}d"


def _is_live(usage: Usage, now: datetime) -> bool:
    return (
        usage.source is Source.LIVE
        and (now - usage.fetched_at).total_seconds() <= LIVE_STALE_AFTER_S
    )


def format_cell(pct: float | None, resets_at: datetime | None, usage: Usage | None, now: datetime) -> str:
    """One usage cell: live "12%  2h10m" (or "12%"), cached "44% · 37d old", else NO_DATA."""
    if pct is None or usage is None:
        return NO_DATA
    shown = f"{math.floor(pct)}%"
    if _is_live(usage, now):
        return shown if resets_at is None else f"{shown}  {format_until(resets_at - now)}"
    return f"{shown} · {format_age(now - usage.fetched_at)} old"

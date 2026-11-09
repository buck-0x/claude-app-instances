"""One snapshot of every instance: process, session, account and usage state per row.

Everything here is a local read (launchers, `ps` lines, cookies, config.json, Claude's
usage history, the TUI's cache) combined with what the caller learned from live
lookups. Reading `ps` is the caller's job: a failed read must never reach this module
as an empty list. Account details (email, org, account UUID) stay out of every repr.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from claude_instances.accounts import (
    AccountRow,
    ExpectedAccounts,
    expected_mismatch,
    last_known_account_uuid,
    shared_account_groups,
)
from claude_instances.cookies import (
    SessionState,
    read_claude_cookies,
    session_created,
    session_state,
)
from claude_instances.launchers import Launcher
from claude_instances.order import apply_order
from claude_instances.processes import (
    MainProcess,
    ProcessLine,
    Running,
    claude_binary,
    main_processes,
    match_instances,
    watchdog_alive,
)
from claude_instances.usage import (
    LIVE_STALE_AFTER_S,
    Source,
    Usage,
    UsageCache,
    account_hash,
    best_cached,
    read_claude_history,
)


@dataclass(frozen=True)
class LiveEntry:
    """The last live usage for an instance, tagged with the account it was fetched for."""

    account: str | None = field(repr=False)
    usage: Usage


@dataclass(frozen=True)
class RejectedMark:
    """The session a server refused: the account and sessionKey creation time at the time."""

    account: str | None = field(repr=False)
    session_created: int | None


@dataclass(frozen=True)
class AccountInfo:
    """Details learned from a live lookup; keyed by account UUID, never by display name."""

    email: str | None = field(default=None, repr=False)
    org: str | None = field(default=None, repr=False)


@dataclass
class InstanceState:
    name: str
    launcher: Launcher
    position: int  # 0-based index in launch order
    running: list[Running]
    session: SessionState
    session_expiry: datetime | None
    session_created: int | None
    account: str | None = field(repr=False)
    email: str | None = field(repr=False)
    org: str | None = field(repr=False)
    expected: str | None = field(repr=False)
    shared_with: list[str]
    expected_mismatch: bool
    usage: Usage | None
    live_error: str | None
    watchdog_alive: bool

    @property
    def pids(self) -> list[int]:
        return [r.pid for r in self.running]

    @property
    def is_running(self) -> bool:
        """Exactly one main process; duplicates are reported by `is_duplicate`."""
        return len(self.running) == 1

    @property
    def is_duplicate(self) -> bool:
        return len(self.running) >= 2

    @property
    def argless(self) -> bool:
        return any(r.argless for r in self.running)

    @property
    def pid(self) -> int | None:
        return self.running[0].pid if len(self.running) == 1 else None


@dataclass
class Snapshot:
    rows: list[InstanceState]
    strays: list[MainProcess]
    taken_at: datetime

    def row(self, name: str) -> InstanceState:
        for row in self.rows:
            if row.name == name:
                return row
        raise KeyError(name)


@dataclass
class Inputs:
    launchers: list[Launcher]
    ps_lines: list[ProcessLine]
    default_data_dir: Path
    order: list[str]
    expected: ExpectedAccounts
    usage_cache: UsageCache
    log_dir: Path
    now: datetime
    live: dict[str, LiveEntry]
    account_info: dict[str, AccountInfo]
    live_errors: dict[str, str]
    rejected: dict[str, RejectedMark]


def _match_all(
    launchers: list[Launcher], ps_lines: list[ProcessLine], default_data_dir: Path
) -> tuple[dict[str, list[Running]], list[MainProcess]]:
    """Match mains per Claude app, since launchers may point at different apps.

    A main is a stray when no launcher using its app owns its data dir.
    """
    by_app: dict[Path, list[Launcher]] = {}
    for launcher in launchers:
        by_app.setdefault(launcher.claude_app, []).append(launcher)
    running: dict[str, list[Running]] = {}
    strays: list[MainProcess] = []
    seen: set[int] = set()
    for app, group in by_app.items():
        procs = main_processes(ps_lines, claude_binary(app))
        matched, unmatched = match_instances(procs, group, default_data_dir)
        for name, runs in matched.items():
            running.setdefault(name, []).extend(runs)
        for proc in unmatched:
            if proc.pid not in seen:
                seen.add(proc.pid)
                strays.append(proc)
    return running, strays


def _usage(
    inp: Inputs, name: str, data_dir: Path, account: str | None, org: str | None
) -> Usage | None:
    entry = inp.live.get(name)
    if entry is not None and entry.account == account:
        age_s = (inp.now - entry.usage.fetched_at).total_seconds()
        if age_s <= LIVE_STALE_AFTER_S:
            return entry.usage
        return replace(entry.usage, source=Source.TUI_CACHE)
    history = None
    if org is not None:
        history = read_claude_history(data_dir, org)
    else:
        # Org-less history is trusted only when the cache names no other account,
        # since the history may then belong to that account too.
        stored = inp.usage_cache.stored_hash(name)
        if stored is None or (account is not None and stored == account_hash(account)):
            history = read_claude_history(data_dir, None)
    return best_cached(inp.usage_cache.get(name, account), history)


def build_snapshot(inp: Inputs) -> Snapshot:
    """Combine the local reads and the caller's live state into rows in launch order."""
    by_name = {l.name: l for l in inp.launchers}
    names = apply_order(list(by_name), inp.order)
    running, strays = _match_all(inp.launchers, inp.ps_lines, inp.default_data_dir)

    rows: list[InstanceState] = []
    for position, name in enumerate(names):
        launcher = by_name[name]
        store = read_claude_cookies(launcher.data_dir)
        state, expiry = session_state(store, inp.now)
        created = session_created(store)
        account = last_known_account_uuid(launcher.data_dir)
        # A refusal holds only for the session it was made on; cookies that have
        # since expired or vanished say more than the old refusal does.
        if state is SessionState.SIGNED_IN and inp.rejected.get(name) == RejectedMark(
                account, created):
            state = SessionState.REJECTED
        info = inp.account_info.get(account) if account is not None else None
        email = info.email if info is not None else None
        org = info.org if info is not None else None
        rows.append(InstanceState(
            name=name,
            launcher=launcher,
            position=position,
            running=running.get(name, []),
            session=state,
            session_expiry=expiry,
            session_created=created,
            account=account,
            email=email,
            org=org,
            expected=inp.expected.get(name),
            shared_with=[],
            expected_mismatch=False,
            usage=_usage(inp, name, launcher.data_dir, account, org),
            live_error=inp.live_errors.get(name),
            watchdog_alive=watchdog_alive(
                name, launcher.script_path, inp.ps_lines, inp.log_dir),
        ))

    account_rows = [AccountRow(r.name, r.account, r.session) for r in rows]
    groups = shared_account_groups(account_rows)
    for row, account_row in zip(rows, account_rows):
        row.shared_with = groups.get(row.name, [])
        row.expected_mismatch = expected_mismatch(account_row, inp.expected)
    return Snapshot(rows=rows, strays=strays, taken_at=inp.now)

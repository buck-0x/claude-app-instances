"""The TUI's backend: what it remembers between refreshes, live lookups and guarded actions.

The UI calls these methods from worker threads and the event loop, so shared state
sits behind a lock and every snapshot works on copies. A failed `ps` read makes
`snapshot()` raise rather than show nothing running; the caller reports it. Account
details (email, org, account UUID) never appear in a message or error string here.
"""

import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from claude_instances import actions as act
from claude_instances.accounts import ExpectedAccounts, last_known_account_uuid
from claude_instances.actions import ActionResult, QuitOutcome, SystemActions
from claude_instances.cookies import read_claude_cookies, session_created
from claude_instances.launchers import Launcher, discover_launchers
from claude_instances.order import apply_order, load_order, move_name, save_order
from claude_instances.paths import EXPECTED_FILE, ORDER_FILE, RESTORE_FILE, USAGE_CACHE_FILE
from claude_instances.signin import SigninFlow, SigninPhase, read_restore
from claude_instances.snapshot import (
    AccountInfo,
    Inputs,
    InstanceState,
    LiveEntry,
    RejectedMark,
    Snapshot,
    build_snapshot,
)
from claude_instances.usage import Lookup, LookupResult, UsageCache

# How long after a successful launch request a second one is refused. A second
# `open -n` would USR1 the watchdog that owns the instance and make a later Cmd+Q
# reopen it.
PENDING_LAUNCH_S = 30

DECRYPT_ERROR = "cannot decrypt cookies (wrong key?)"

KeyStatus = Literal["none", "unverified", "ok", "bad"]


class Backend:
    def __init__(
        self,
        apps_dirs: list[Path],
        config_dir: Path,
        log_dir: Path,
        support_dir: Path,
        actions: SystemActions,
        lookup: Lookup | None,
        default_data_dir: Path,
        default_claude_app: Path,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.apps_dirs = apps_dirs
        self.config_dir = config_dir
        self.log_dir = log_dir
        self.support_dir = support_dir
        self.actions = actions
        self.lookup = lookup
        self.default_data_dir = default_data_dir
        self.default_claude_app = default_claude_app
        self.sleep = sleep
        self.now = now
        self.monotonic = monotonic
        self.cached_only = lookup is None
        self.key_status: KeyStatus = "none" if lookup is None else "unverified"
        self.usage_cache = UsageCache.load(config_dir / USAGE_CACHE_FILE)
        self.live: dict[str, LiveEntry] = {}
        self.account_info: dict[str, AccountInfo] = {}
        self.live_errors: dict[str, str] = {}
        self.rejected: dict[str, RejectedMark] = {}
        self.pending_launches: dict[str, float] = {}
        # Set by each snapshot when the expected-accounts file cannot be read.
        self.expected_error: str | None = None
        self._lock = threading.Lock()  # guards the live-state dicts and the cache
        self._launch_lock = threading.Lock()  # makes the pending check and launch atomic

    # ---- reads --------------------------------------------------------------------

    def _launchers(self) -> list[Launcher]:
        return discover_launchers(self.apps_dirs, self.default_claude_app)

    def _launcher(self, name: str) -> Launcher:
        for launcher in self._launchers():
            if launcher.name == name:
                return launcher
        raise KeyError(name)

    def _load_expected(self) -> ExpectedAccounts:
        """Raises ValueError (corrupt) or OSError (unreadable)."""
        return ExpectedAccounts.load(self.config_dir / EXPECTED_FILE)

    def snapshot(self) -> Snapshot:
        """A fresh snapshot. Raises when `ps` cannot be read: never "nothing running"."""
        launchers = self._launchers()
        ps_lines = self.actions.processes()
        try:
            expected = self._load_expected()
            self.expected_error = None
        except (ValueError, OSError) as e:
            expected = ExpectedAccounts()
            self.expected_error = f"expected accounts file unreadable ({type(e).__name__})"
        order = load_order(self.config_dir / ORDER_FILE)
        with self._lock:
            inputs = Inputs(
                launchers=launchers,
                ps_lines=ps_lines,
                default_data_dir=self.default_data_dir,
                order=order,
                expected=expected,
                usage_cache=self.usage_cache,
                log_dir=self.log_dir,
                now=self.now(),
                live=dict(self.live),
                account_info=dict(self.account_info),
                live_errors=dict(self.live_errors),
                rejected=dict(self.rejected),
            )
            snapshot = build_snapshot(inputs)
        with self._launch_lock:
            for row in snapshot.rows:
                if row.pids:
                    self.pending_launches.pop(row.name, None)
        return snapshot

    def _row(self, name: str) -> InstanceState | None:
        try:
            return self.snapshot().row(name)
        except KeyError:
            return None

    # ---- live lookups -------------------------------------------------------------

    def live_lookup(self, name: str) -> LookupResult:
        """Run one live lookup for `name` and record the outcome. Never raises."""
        if self.lookup is None:
            return LookupResult(usage=None, error="live lookups are off (no key)")
        step = "instance read"
        result: LookupResult | None = None
        try:
            # The account is read before the lookup, so usage fetched across an
            # account switch is tagged with the old account and never shown.
            data_dir = self._launcher(name).data_dir
            account = last_known_account_uuid(data_dir)
            created = session_created(read_claude_cookies(data_dir))
            step = "lookup"
            result = self.lookup.lookup(data_dir)
            step = "result"
            self._record(name, account, created, result)
            if result.usage is not None:
                step = "cache write"
                with self._lock:
                    self.usage_cache.put(name, account, result.usage)
                    self.usage_cache.save(self.config_dir / USAGE_CACHE_FILE)
        except Exception as e:
            message = f"{step} failed ({type(e).__name__})"
            with self._lock:
                self.live_errors[name] = message
            if result is None:
                result = LookupResult(usage=None, error=message)
        return result

    def _record(self, name: str, account: str | None, created: int | None,
                result: LookupResult) -> None:
        with self._lock:
            if result.usage is not None:
                self.live[name] = LiveEntry(account, result.usage)
                if account is not None:
                    self.account_info[account] = AccountInfo(result.email, result.org)
                self.rejected.pop(name, None)
                self.live_errors.pop(name, None)
                self.key_status = "ok"
            elif result.rejected:
                # The server saw a decrypted session, so the key works.
                self.rejected[name] = RejectedMark(account, created)
                self.live_errors.pop(name, None)
                self.key_status = "ok"
            else:
                self.live_errors[name] = result.error or "no usage returned"
                if result.error == DECRYPT_ERROR and self.key_status != "ok":
                    self.key_status = "bad"

    # ---- launch -------------------------------------------------------------------

    def _launch_launcher(self, launcher: Launcher) -> ActionResult:
        with self._launch_lock:
            requested = self.pending_launches.get(launcher.name)
            if requested is not None:
                age = self.monotonic() - requested
                if age < PENDING_LAUNCH_S:
                    return ActionResult(
                        False, f"launch requested {int(age)}s ago; waiting for it to appear")
            result = act.guarded_launch(launcher, self.actions, self.default_data_dir)
            if result.ok:
                self.pending_launches[launcher.name] = self.monotonic()
            return result

    def launch(self, name: str) -> ActionResult:
        try:
            launcher = self._launcher(name)
        except KeyError:
            return ActionResult(False, f"no launcher named {name}")
        return self._launch_launcher(launcher)

    def launch_all(self) -> list[tuple[str, ActionResult]]:
        """Launch every closed row; skipped rows come back as not-ok entries."""
        return act.launch_all(self.snapshot(), self.actions, self.default_data_dir,
                              sleep=self.sleep, launch_one=self._launch_launcher)

    # ---- other actions ------------------------------------------------------------

    def rearm(self, name: str) -> ActionResult:
        row = self._row(name)
        if row is None:
            return ActionResult(False, f"no launcher named {name}")
        return act.rearm_watchdog(row, self.actions, self.default_data_dir)

    def quit(self, name: str) -> tuple[QuitOutcome, str]:
        row = self._row(name)
        if row is None:
            return QuitOutcome.NOT_RUNNING, f"no launcher named {name}"
        return act.quit_instance(row, self.actions, self.sleep,
                                 default_data_dir=self.default_data_dir, log_dir=self.log_dir)

    def front(self, name: str) -> ActionResult:
        row = self._row(name)
        if row is None:
            return ActionResult(False, f"no launcher named {name}")
        if row.is_duplicate:
            return ActionResult(False, f"{name} has more than one process running")
        if row.pid is None:
            return ActionResult(False, f"{name} is not running")
        return self.actions.bring_to_front(row.pid)

    def move(self, name: str, delta: int) -> None:
        """Move `name` `delta` places in the launch order and save the order file."""
        path = self.config_dir / ORDER_FILE
        current = apply_order([l.name for l in self._launchers()], load_order(path))
        moved = move_name(current, name, delta)
        if moved != current:
            save_order(path, moved)

    def mark_expected(self, name: str) -> ActionResult:
        """Record `name`'s current account as its expected one."""
        try:
            data_dir = self._launcher(name).data_dir
        except KeyError:
            return ActionResult(False, f"no launcher named {name}")
        account = last_known_account_uuid(data_dir)
        if account is None:
            return ActionResult(False, f"{name} has no known account")
        try:
            expected = self._load_expected()
        except (ValueError, OSError) as e:
            # Never replace a file we could not read with a near-empty map.
            return ActionResult(
                False, f"expected accounts file unreadable ({type(e).__name__}); not changed")
        expected.set(name, account)
        try:
            expected.save(self.config_dir / EXPECTED_FILE)
        except OSError as e:
            return ActionResult(False, f"could not save expected accounts ({type(e).__name__})")
        return ActionResult(True, f"recorded the expected account for {name}")

    # ---- sign-in support ----------------------------------------------------------

    def pending_restore(self) -> list[str]:
        """Names an interrupted sign-in left to relaunch; empty while its owner still runs."""
        owner, names = read_restore(self.config_dir / RESTORE_FILE)
        if owner is not None and self.actions.is_alive(owner):
            return []
        return names

    def make_signin_flow(self, on_phase: Callable[[SigninPhase, str], None]) -> SigninFlow:
        return SigninFlow(self.actions, self.snapshot, self.config_dir, self.default_data_dir,
                          sleep=self.sleep, on_phase=on_phase, log_dir=self.log_dir)

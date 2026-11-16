"""When to run the next live usage lookup: serial, one instance at a time, with backoff.

Pure and clock-injected: no threads and no Textual. The scheduler is not
thread-safe by design; callers use it from the event loop only (the app calls
`start` before spawning the lookup worker and `finish` through
`call_from_thread`).
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

from claude_instances.usage import LIVE_INTERVAL_S

STATE_INTERVAL_S = 10


def backoff_after(failures: int, base: int = LIVE_INTERVAL_S, cap: int = 3600) -> int:
    """Seconds to wait after `failures` consecutive failed lookups: doubles each time, capped."""
    return min(base * 2**failures, cap)


@dataclass
class _State:
    due: float  # monotonic time the next lookup is due; -inf means right away
    failures: int = 0


class LiveScheduler:
    """Picks which candidate's live lookup runs next; at most one is in flight at a time.

    Candidates are the rows with a session cookie (signed in or rejected), so a
    rejected row keeps being retried at backoff cadence.
    """

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._states: dict[str, _State] = {}
        self._in_flight: str | None = None

    def set_candidates(self, names: list[str]) -> None:
        """Replace the candidate list in order; new names are due now, dropped names are forgotten."""
        self._states = {
            name: self._states.get(name) or _State(due=float("-inf")) for name in names
        }

    def request_now(self, name: str) -> None:
        """Make `name` due right away; ignored if it is not a candidate."""
        state = self._states.get(name)
        if state is not None:
            state.due = float("-inf")

    def next_due(self) -> str | None:
        """The candidate with the earliest due time that has passed, ties in candidate order.

        None while a lookup is in flight or when nothing is due yet.
        """
        if self._in_flight is not None:
            return None
        now = self._now()
        due = [(state.due, name) for name, state in self._states.items() if state.due <= now]
        if not due:
            return None
        return min(due, key=lambda item: item[0])[1]

    def start(self, name: str) -> None:
        """Mark `name`'s lookup as in flight."""
        self._in_flight = name

    def finish(self, name: str, ok: bool) -> None:
        """Record a lookup's outcome and schedule `name`'s next one; always clears in-flight."""
        self._in_flight = None
        state = self._states.get(name)
        if state is None:  # dropped while its lookup ran
            return
        if ok:
            state.failures = 0
            state.due = self._now() + LIVE_INTERVAL_S
        else:
            state.failures += 1
            state.due = self._now() + backoff_after(state.failures)

"""Modal screens for the instances TUI: confirmations, details, sign-in and restore offer.

These screens only show text and report the user's answer; the app runs every action.
Text is rendered as plain `rich.text.Text`, never as markup, so names and messages
with brackets show as typed.
"""

from collections import deque
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static

from claude_instances.accounts import short_id
from claude_instances.cookies import SessionState
from claude_instances.launchers import rebuild_command
from claude_instances.snapshot import InstanceState

LOG_TAIL_LINES = 20
SESSION_CAVEAT = (
    "A present cookie means the app believes it is signed in; a server-side revocation"
    " shows only when the app next talks to the server."
)

_MODAL_CSS = """
ModalScreen {
    align: center middle;
}
ModalScreen > Vertical {
    width: 90%;
    max-width: 110;
    height: auto;
    max-height: 90%;
    border: round $accent;
    background: $surface;
    padding: 1 2;
}
ModalScreen .hint {
    color: $text-muted;
    margin-top: 1;
}
"""


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question; dismisses with True on `y`, False on `n` or escape."""

    DEFAULT_CSS = _MODAL_CSS
    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n", "answer(False)", "No"),
        Binding("escape", "answer(False)", "No", show=False),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(self.question))
            yield Static(Text("y yes    n no"), classes="hint")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


def _tail(path: Path, lines: int = LOG_TAIL_LINES) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return "".join(deque(f, maxlen=lines)).rstrip("\n") or "(log is empty)"
    except FileNotFoundError:
        return "(no log yet)"
    except OSError as e:
        return f"(log unreadable: {type(e).__name__})"


def _who(row: InstanceState) -> str:
    if row.email:
        return row.email
    return short_id(row.account) if row.account else "unknown"


def _expected(row: InstanceState) -> str:
    """The expected account: its email when it is the signed-in one, else a short id."""
    if row.expected is None:
        return "not set"
    if row.expected == row.account:
        return row.email or short_id(row.expected)
    if row.account is not None:
        return f"{short_id(row.expected)} (not the signed-in account)"
    return short_id(row.expected)


def details_text(row: InstanceState, support_dir: Path, log_dir: Path) -> str:
    """Everything the details panel shows for one row."""
    launcher = row.launcher
    if row.session_expiry is not None:
        expiry = row.session_expiry.astimezone().strftime("%Y-%m-%d %H:%M")
    elif row.session is SessionState.SIGNED_IN:
        expiry = "browser session (no expiry)"
    else:
        expiry = "no session cookie"
    rebuild = rebuild_command(launcher, support_dir) or "cannot be rebuilt with new-claude"
    lines = [
        row.name,
        "",
        f"Account:          {_who(row)} ({row.session.value.replace('_', ' ')})",
        f"Expected account: {_expected(row)}",
        f"Data folder:      {launcher.data_dir}",
        f"Launcher:         {launcher.app_path}",
        f"Session expires:  {expiry}",
        f"Watchdog:         {'alive' if row.watchdog_alive else 'not running'}",
        f"Rebuild:          {rebuild}",
    ]
    if not launcher.is_watchdog:
        lines.append("Note:             old one-shot launcher")
    elif not launcher.has_argless_rule:
        lines.append("Note:             launcher predates the argless rule")
    for path in row.shadowed:
        lines.append(f"Same name:        {path} (ignored; the launcher above is used)")
    if row.live_error:
        lines.append(f"Live lookup:      {row.live_error}")
    lines += ["", SESSION_CAVEAT, "", f"Last {LOG_TAIL_LINES} watchdog log lines:",
              _tail(log_dir / f"{row.name}.log")]
    return "\n".join(lines)


class DetailsScreen(ModalScreen[None]):
    DEFAULT_CSS = _MODAL_CSS
    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("d", "close", "Close", show=False),
        Binding("q", "close", "Close", show=False),
    ]

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(self.text))
            yield Static(Text("escape close"), classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)


class SigninScreen(ModalScreen[None]):
    """Shows the running sign-in's phase and warnings; escape cancels it.

    The app dismisses it when the sign-in thread finishes.
    """

    DEFAULT_CSS = _MODAL_CSS
    BINDINGS = [Binding("escape", "cancel", "Cancel sign-in")]

    def __init__(self, target: str, on_cancel) -> None:
        super().__init__()
        self.target = target
        self.on_cancel = on_cancel
        self.phase_text = "starting"
        self.warnings: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(Text(f"Signing in {self.target}"))
            yield Static(Text(self.phase_text), id="phase")
            yield Static(Text(""), id="warnings")
            yield Static(Text("escape cancel and relaunch what was quit"), classes="hint")

    def show_phase(self, text: str) -> None:
        self.phase_text = text
        if self.is_mounted:
            self.query_one("#phase", Static).update(Text(text))

    def add_warning(self, text: str) -> None:
        self.warnings.append(text)
        if self.is_mounted:
            self.query_one("#warnings", Static).update(
                Text("\n".join(f"⚠ {w}" for w in self.warnings)))

    def action_cancel(self) -> None:
        self.on_cancel()
        self.show_phase("Cancelling; relaunching what was quit...")


class RestoreOfferScreen(ModalScreen[bool]):
    """Offers to relaunch what an interrupted sign-in quit; dismisses with the answer."""

    DEFAULT_CSS = _MODAL_CSS
    BINDINGS = [
        Binding("y", "answer(True)", "Relaunch"),
        Binding("n", "answer(False)", "Not now"),
        Binding("escape", "answer(False)", "Not now", show=False),
    ]

    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self.names = names

    def compose(self) -> ComposeResult:
        n = len(self.names)
        noun = "instance" if n == 1 else "instances"
        with Vertical():
            yield Static(Text(f"Relaunch {n} {noun} quit by an interrupted sign-in?"))
            yield Static(Text("\n".join(f"  {name}" for name in self.names)))
            yield Static(Text("y relaunch in order    n not now (offered again next start)"),
                         classes="hint")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)

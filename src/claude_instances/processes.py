"""Match running Claude main processes to launchers, and check watchdog liveness.

Stricter than `instance_pid` / `instance_running_for` in bin/new-claude.sh: helper
processes (`--type=`) never count, and a main process without `--user-data-dir=` is
"argless" only when the binary ran with no arguments or with flags only. Argless
mains use the default data folder, so they belong to the launcher whose data dir is
that folder. A wrong match here could let the TUI open a duplicate instance.
"""

import os
import plistlib
import re
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from claude_instances.launchers import Launcher

_PS_LINE = re.compile(r"^\s*(\d+)\s+(.*)$")
_TYPE_FLAG = "--type="
_DATA_DIR_FLAG = "--user-data-dir="

# How many trailing watchdog log lines decide whether it waits for an update install.
# `new-claude --launch-all` reads the same number with the same rule.
UPDATE_WAIT_LOG_LINES = 200
_UPDATE_WAIT_START = re.compile(r"update pending \(.*but not installed")
# Lines a watchdog in update wait may log without leaving it.
_UPDATE_WAIT_NOISE = ("still waiting for update install",
                      "launcher opened while instance is down")


@dataclass(frozen=True)
class ProcessLine:
    pid: int
    command: str


@dataclass(frozen=True)
class MainProcess:
    pid: int
    data_dir: Path | None  # None: argless, so it uses the default data folder


@dataclass
class Running:
    pid: int
    argless: bool


def parse_ps(text: str) -> list[ProcessLine]:
    """Parse `ps -axo pid=,command=` output; the command is kept whole, spaces and all."""
    lines = []
    for raw in text.splitlines():
        match = _PS_LINE.match(raw)
        if match:
            lines.append(ProcessLine(int(match.group(1)), match.group(2)))
    return lines


def list_processes() -> list[ProcessLine]:
    """Every process on the machine. Raises if `ps` fails, so a failed read is never
    mistaken for "nothing running"."""
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        capture_output=True, text=True, errors="replace", check=True,
    )
    return parse_ps(result.stdout)


def claude_binary(claude_app: Path) -> str:
    """`<app>/Contents/MacOS/<CFBundleExecutable>`, falling back to `Claude`."""
    exe = "Claude"
    try:
        with open(claude_app / "Contents/Info.plist", "rb") as f:
            info = plistlib.load(f)
        value = info.get("CFBundleExecutable") if isinstance(info, dict) else None
        if isinstance(value, str) and value:
            exe = value
    except Exception:  # missing or malformed plist: use the default name
        pass
    return f"{claude_app}/Contents/MacOS/{exe}"


def _after_binary(command: str, binary: str) -> str | None:
    """Text after the first whole-token occurrence of `binary`, or None if absent.

    The binary must start the command or follow a space, and end the command or be
    followed by a space (so `.../Claude` never matches `.../ClaudeX`).
    """
    start = command.find(binary)
    while start != -1:
        end = start + len(binary)
        if (start == 0 or command[start - 1] == " ") and (
            end == len(command) or command[end] == " "
        ):
            return command[end:]
        start = command.find(binary, start + 1)
    return None


def _data_dir_value(command: str) -> str | None:
    """The `--user-data-dir=` value: up to the end of the line or the next `" --"`."""
    i = command.find(_DATA_DIR_FLAG)
    if i == -1:
        return None
    value = command[i + len(_DATA_DIR_FLAG):]
    stop = value.find(" --")
    return value if stop == -1 else value[:stop]


def main_processes(lines: list[ProcessLine], binary: str) -> list[MainProcess]:
    """Claude main processes for `binary`, each with its data dir or None if argless.

    Lines with `--type=` are helpers and never count. A line with the binary but no
    `--user-data-dir=` counts only when the binary has no arguments or flags only.
    """
    procs = []
    for line in lines:
        if _TYPE_FLAG in line.command:
            continue
        rest = _after_binary(line.command, binary)
        if rest is None:
            continue
        data_dir = _data_dir_value(line.command)
        if data_dir is not None:
            procs.append(MainProcess(line.pid, Path(data_dir)))
        elif rest == "" or rest.startswith(" -"):
            procs.append(MainProcess(line.pid, None))
    return procs


def is_default_dir(data_dir: Path, default_data_dir: Path) -> bool:
    """True when `data_dir` is, or resolves to, the default data folder.

    A plain Claude (no `--user-data-dir`, as ShipIt relaunches it after an update)
    runs on the default folder, so it belongs to the launcher whose folder this is.
    Symlinks are resolved as the zsh `:A` modifier does in the launcher.
    """
    if data_dir == default_data_dir:
        return True
    return os.path.realpath(data_dir) == os.path.realpath(default_data_dir)


def match_instances(
    procs: list[MainProcess], launchers: list[Launcher], default_data_dir: Path
) -> tuple[dict[str, list[Running]], list[MainProcess]]:
    """Map each launcher name to its running mains, and list mains matching none.

    A main matches every launcher with the same data dir; argless mains match the
    launcher(s) whose data dir is `default_data_dir` (see `is_default_dir`). More
    than one match per launcher means duplicates are running.
    """
    by_name: dict[str, list[Running]] = {l.name: [] for l in launchers}
    strays = []
    default_owners = [l for l in launchers if is_default_dir(l.data_dir, default_data_dir)]
    for proc in procs:
        if proc.data_dir is None:
            owners = default_owners
        else:
            owners = [l for l in launchers if l.data_dir == proc.data_dir]
        for launcher in owners:
            by_name[launcher.name].append(Running(proc.pid, argless=proc.data_dir is None))
        if not owners:
            strays.append(proc)
    return by_name, strays


def is_watchdog_command(command: str, script: str) -> bool:
    """True when `command` is zsh running the launcher script `script`.

    macOS runs the launcher's `#!/bin/zsh` as `<zsh> <script>`: the first word must be
    a zsh interpreter (`zsh` or a path ending `/zsh`) and the rest of the line the
    whole script path, alone or followed by a space. So `less <script>`, `vim -R
    <script>` or `/bin/zsh <script>.bak` never count, and `/Applications/X.app/...`
    never matches `~/Applications/X.app/...`. `new-claude.sh` uses the same rule.
    """
    interpreter, sep, rest = command.partition(" ")
    if not sep or interpreter.rsplit("/", 1)[-1] != "zsh":
        return False
    return rest == script or rest.startswith(script + " ")


def watchdog_pids(lines: list[ProcessLine], script_path: Path) -> list[int]:
    """Pids of zsh processes running the launcher script (see `is_watchdog_command`)."""
    script = str(script_path)
    return [line.pid for line in lines if is_watchdog_command(line.command, script)]


def log_says_update_wait(log_path: Path) -> bool:
    """True when a watchdog's log says it waits for a Claude update to install.

    The watchdog logs `update pending (...) but not installed` as it starts waiting,
    then only heartbeats (and notes from a launcher opened meanwhile) until it
    reopens or exits, each of which logs another line. So the wait holds when the
    last log line other than those is the `update pending` one. Callers also check
    that the watchdog is alive: a dead one waits for nothing.
    """
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=UPDATE_WAIT_LOG_LINES)
    except OSError:
        return False
    for line in reversed(tail):
        if any(noise in line for noise in _UPDATE_WAIT_NOISE):
            continue
        return _UPDATE_WAIT_START.search(line) is not None
    return False


def watchdog_alive(
    name: str, script_path: Path, lines: list[ProcessLine], log_dir: Path
) -> bool:
    """True when `<log_dir>/<name>.lock` is a symlink to the pid of a live watchdog."""
    try:
        target = os.readlink(log_dir / f"{name}.lock")
    except OSError:
        return False
    try:
        pid = int(target)
    except ValueError:
        return False
    return pid in watchdog_pids(lines, script_path)

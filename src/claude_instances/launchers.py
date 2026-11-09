"""Discover the launcher apps `new-claude` creates.

A launcher is any `*.app` whose `CFBundleIdentifier` starts with `local.launcher.`.
Its `Contents/MacOS/launcher` script names the data dir and the Claude app, in one of
three shapes: the current header (`CLAUDE_APP=`, `DATA_DIR=`, `DEFAULT_DATA_DIR=`,
`INSTANCE_NAME=`), the older watchdog header without `DEFAULT_DATA_DIR=`, or an old
one-shot script with an inline `open -n -a "..." --args --user-data-dir="..."`.
Parsing mirrors `launcher_data_dir` and `launcher_claude_app` in bin/new-claude.sh.
"""

import plistlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

BUNDLE_PREFIX = "local.launcher."
SCRIPT_RELPATH = "Contents/MacOS/launcher"

_DATA_DIR_LINE = re.compile(r'^DATA_DIR="(.*)"$', re.MULTILINE)
_DEFAULT_DATA_DIR_LINE = re.compile(r'^DEFAULT_DATA_DIR="(.*)"$', re.MULTILINE)
_CLAUDE_APP_LINE = re.compile(r'^CLAUDE_APP="(.*)"$', re.MULTILINE)
_INLINE_DATA_DIR = re.compile(r'--user-data-dir="([^"]*)"')
_INLINE_CLAUDE_APP = re.compile(r'open -n -a "([^"]*)"')


@dataclass(frozen=True)
class Launcher:
    name: str
    app_path: Path
    script_path: Path
    data_dir: Path
    claude_app: Path
    is_watchdog: bool  # has a DATA_DIR= header line, so it holds the per-instance lock
    has_argless_rule: bool  # has a DEFAULT_DATA_DIR= header line


def _first(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1) if match else ""


def parse_launcher_script(
    text: str, default_claude_app: Path
) -> tuple[Path | None, Path, bool, bool]:
    """Return (data_dir, claude_app, is_watchdog, has_argless_rule) for a launcher script.

    An empty value counts as missing, falling back as the shell helpers do.
    """
    data_dir = _first(_DATA_DIR_LINE, text) or _first(_INLINE_DATA_DIR, text)
    claude_app = _first(_CLAUDE_APP_LINE, text) or _first(_INLINE_CLAUDE_APP, text)
    return (
        Path(data_dir) if data_dir else None,
        Path(claude_app) if claude_app else default_claude_app,
        _DATA_DIR_LINE.search(text) is not None,
        _DEFAULT_DATA_DIR_LINE.search(text) is not None,
    )


def _read_launcher(app: Path, default_claude_app: Path) -> Launcher | None:
    try:
        with open(app / "Contents/Info.plist", "rb") as f:
            info = plistlib.load(f)
    except Exception:  # plistlib raises ExpatError, AttributeError etc. on bad input
        return None
    if not isinstance(info, dict):
        return None
    bundle_id = info.get("CFBundleIdentifier")
    if not isinstance(bundle_id, str) or not bundle_id.startswith(BUNDLE_PREFIX):
        return None
    script = app / SCRIPT_RELPATH
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    data_dir, claude_app, is_watchdog, has_argless_rule = parse_launcher_script(
        text, default_claude_app)
    if data_dir is None:
        return None
    name = info.get("CFBundleDisplayName")
    if not isinstance(name, str) or not name:
        name = app.stem
    return Launcher(name, app, script, data_dir, claude_app, is_watchdog, has_argless_rule)


def discover_launchers(
    apps_dirs: list[Path], default_claude_app: Path = Path("/Applications/Claude.app")
) -> list[Launcher]:
    """All launcher apps directly inside `apps_dirs`, sorted by name (codepoint order).

    Missing dirs, unreadable plists and scripts without a data dir are skipped.
    """
    found = []
    for apps_dir in apps_dirs:
        for app in apps_dir.glob("*.app"):
            launcher = _read_launcher(app, default_claude_app)
            if launcher is not None:
                found.append(launcher)
    return sorted(found, key=lambda l: (l.name, str(l.app_path)))


def rebuild_command(launcher: Launcher, support_dir: Path) -> str | None:
    """The `new-claude` command that recreates `launcher` with the same data folder.

    None when the data dir is not directly under `support_dir`, since `new-claude`
    can only build data dirs there.
    """
    if launcher.data_dir.parent != support_dir:
        return None
    parts = ["new-claude", launcher.name, "--dir", launcher.data_dir.name]
    if launcher.app_path.parent != Path("/Applications"):
        parts += ["--apps", str(launcher.app_path.parent)]
    return " ".join(shlex.quote(p) for p in parts)

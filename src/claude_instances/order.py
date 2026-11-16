"""The launch order file: one launcher name per line, read by the TUI and the zsh tools."""

import os
import tempfile
from pathlib import Path


def load_order(path: Path) -> list[str]:
    """Return the names listed in `path`, one per line, skipping blank lines; a missing file gives []."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [line for line in text.splitlines() if line.strip()]


def save_order(path: Path, names: list[str]) -> None:
    """Write `names` to `path` atomically, one per line, dropping repeats and creating the parent folder."""
    path.parent.mkdir(parents=True, exist_ok=True)
    unique = list(dict.fromkeys(names))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(f"{name}\n" for name in unique))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def apply_order(names: list[str], order: list[str]) -> list[str]:
    """Order `names` by `order` (unknown and repeated entries skipped), then the rest in codepoint order."""
    known = set(names)
    listed = [name for name in dict.fromkeys(order) if name in known]
    placed = set(listed)
    return listed + sorted(name for name in known if name not in placed)


def move_name(ordered: list[str], name: str, delta: int) -> list[str]:
    """Return a new list with `name` moved `delta` positions, clamped to the ends; unchanged if absent."""
    result = list(ordered)
    if name not in result:
        return result
    index = result.index(name)
    result.pop(index)
    target = max(0, min(len(result), index + delta))
    result.insert(target, name)
    return result

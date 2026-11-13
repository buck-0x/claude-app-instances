"""Atomic writes of small private JSON files."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_private_json(path: Path, data: Any) -> None:
    """Write `data` as JSON atomically with mode 0600, creating the parent folder if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

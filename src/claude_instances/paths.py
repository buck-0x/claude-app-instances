"""Filesystem locations used by claude-instances.

The config dir and the default Claude data dir can be overridden with env vars
(also read by the zsh scripts), mainly so tests never touch real user data.
"""

import os
from pathlib import Path

ORDER_FILE = "order.txt"
EXPECTED_FILE = "expected-accounts.json"
USAGE_CACHE_FILE = "usage-cache.json"
RESTORE_FILE = "signin-restore.txt"


def config_dir() -> Path:
    override = os.environ.get("CLAUDE_INSTANCES_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config/claude-instances"


def support_dir() -> Path:
    return Path.home() / "Library/Application Support"


def default_data_dir() -> Path:
    override = os.environ.get("CLAUDE_LAUNCHER_DEFAULT_DATA_DIR")
    if override:
        return Path(override)
    return support_dir() / "Claude"


def log_dir() -> Path:
    return Path.home() / "Library/Logs/claude-instances"


def apps_dirs() -> list[Path]:
    return [Path("/Applications"), Path.home() / "Applications"]

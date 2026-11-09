"""Which Claude account each instance is signed in to, and checks against expectations.

The account identity comes from `lastKnownAccountUuid` in the instance's
`config.json`, read without writing back. Expected accounts are a small JSON map of
instance name to account UUID that the user records; it is saved with mode 0600.
"""

import json
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from claude_instances.cookies import SessionState

CONFIG_FILENAME = "config.json"
ACCOUNT_KEY = "lastKnownAccountUuid"
SHORT_ID_LEN = 8

# States whose account is in real use: a server refusing the session still names it.
_SHARING_STATES = frozenset({SessionState.SIGNED_IN, SessionState.REJECTED})


def last_known_account_uuid(data_dir: Path) -> str | None:
    """The account UUID recorded in `<data_dir>/config.json`, or None if absent or malformed."""
    try:
        data = json.loads((data_dir / CONFIG_FILENAME).read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(ACCOUNT_KEY)
    return value if isinstance(value, str) and value else None


def short_id(uuid: str) -> str:
    return uuid[:SHORT_ID_LEN]


class ExpectedAccounts:
    """Instance name to the account UUID the user expects it to be signed in to."""

    def __init__(self, accounts: dict[str, str] | None = None) -> None:
        self._accounts: dict[str, str] = dict(accounts or {})

    @classmethod
    def load(cls, path: Path) -> "ExpectedAccounts":
        """Read the map; a missing file is empty. Non-string entries are ignored."""
        try:
            data = json.loads(path.read_bytes())
        except FileNotFoundError:
            return cls()
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a JSON object")
        return cls({k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)})

    def save(self, path: Path) -> None:
        """Write the map atomically with mode 0600, creating the parent folder if needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._accounts, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def get(self, name: str) -> str | None:
        return self._accounts.get(name)

    def set(self, name: str, uuid: str) -> None:
        self._accounts[name] = uuid


@dataclass
class AccountRow:
    name: str
    account: str | None
    state: SessionState


def shared_account_groups(rows: list[AccountRow]) -> dict[str, list[str]]:
    """Each instance name mapped to the other instances on the same account.

    Only SIGNED_IN and REJECTED rows with a known account count; names with no
    other instance on their account are left out.
    """
    by_account: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        if row.account is not None and row.state in _SHARING_STATES:
            by_account[row.account].append(row.name)
    groups: dict[str, list[str]] = {}
    for names in by_account.values():
        if len(names) < 2:
            continue
        for name in names:
            groups[name] = [other for other in names if other != name]
    return groups


def expected_mismatch(row: AccountRow, expected: ExpectedAccounts) -> bool:
    """True when an expectation is recorded for the row and its known account differs."""
    want = expected.get(row.name)
    if want is None or row.account is None:
        return False
    return row.account != want

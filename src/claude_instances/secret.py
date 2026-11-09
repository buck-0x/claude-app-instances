"""In-memory secret holder that never shows its value, and key delivery over a pipe.

Secrets (Safe Storage key, decrypted cookies, raw API responses) live only inside
`Secret`; its repr and str are redacted, so it can be logged, printed or put in a
container without leaking. Call `reveal()` only at the point of use.
"""

import os

_REDACTED = "Secret(<redacted>)"


class Secret:
    __slots__ = ("_value",)

    def __init__(self, value: str | bytes) -> None:
        self._value = value

    def reveal(self) -> str | bytes:
        return self._value

    def __repr__(self) -> str:
        return _REDACTED

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return self._value == other._value

    __hash__ = None  # mutable-equality semantics; never used as a dict key

    def __len__(self) -> int:
        return len(self._value)


def read_secret_fd(fd: int) -> Secret | None:
    """Read `fd` to EOF as UTF-8, strip one trailing newline and close it.

    Returns None if `fd` is not open or yields nothing.
    """
    try:
        os.fstat(fd)
    except OSError:
        return None
    chunks = []
    try:
        while chunk := os.read(fd, 65536):
            chunks.append(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if data.endswith(b"\n"):
        data = data[:-1]
    if not data:
        return None
    return Secret(data.decode("utf-8"))

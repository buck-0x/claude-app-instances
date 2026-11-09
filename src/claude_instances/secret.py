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

    # Defining __eq__ already disables hashing; stated explicitly so a Secret is
    # never usable as a dict key or set member (that would invite hashing secrets).
    __hash__ = None

    def __len__(self) -> int:
        return len(self._value)


def read_secret_fd(fd: int) -> Secret | None:
    """Read `fd` to EOF as UTF-8, strip one trailing newline and close it.

    Returns None if `fd` is not open or yields nothing. Raises ValueError with a
    fixed message (no secret bytes, no chained cause) if the payload is not UTF-8.
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
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    # Drop the raw bytes before any raise so a traceback that prints locals
    # cannot show them; the UnicodeDecodeError (whose .object holds the full
    # payload) is already gone because it is never chained.
    del data, chunks
    if text is None:
        raise ValueError("secret read from fd is not valid UTF-8") from None
    return Secret(text)

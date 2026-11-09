"""Decrypt Chromium (macOS) cookie values with the Safe Storage password.

macOS Chromium encrypts each cookie as `b"v10"` + AES-128-CBC(PKCS7(value)) with a
key derived from the Keychain "Safe Storage" password and a fixed IV of 16 spaces.
From Cookies DB version 24 the plaintext starts with `sha256(host_key)`, binding the
value to its host. Keys and plaintexts stay inside `Secret`; every failure raises a
ValueError with a fixed message and no chained cause, so nothing secret leaks.
"""

import hashlib

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from claude_instances.cookies import CookieRow, CookieStore
from claude_instances.secret import Secret

_SALT = b"saltysalt"
_ITERATIONS = 1003
_KEY_LEN = 16
_PREFIX = b"v10"
_IV = b" " * 16
_DIGEST_FROM_VERSION = 24
_DIGEST_LEN = 32


def derive_key(password: Secret) -> Secret:
    """PBKDF2-HMAC-SHA1 of the Safe Storage password, as Chromium does on macOS."""
    pw = password.reveal()
    if isinstance(pw, str):
        pw = pw.encode("utf-8")
    return Secret(hashlib.pbkdf2_hmac("sha1", pw, _SALT, _ITERATIONS, _KEY_LEN))


def _plaintext(
    blob: bytes, key: bytes, host_key: str, db_version: int
) -> tuple[str | None, str]:
    """(value, "") on success, else (None, fixed reason); exceptions are swallowed."""
    if not blob.startswith(_PREFIX):
        return None, "unsupported cookie encryption prefix"
    try:
        dec = Cipher(algorithms.AES(key), modes.CBC(_IV)).decryptor()
        padded = dec.update(blob[len(_PREFIX):]) + dec.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        raw = unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        return None, "cookie ciphertext or padding is invalid"
    if db_version >= _DIGEST_FROM_VERSION:
        if raw[:_DIGEST_LEN] != hashlib.sha256(host_key.encode("utf-8")).digest():
            return None, "cookie host digest does not match"
        raw = raw[_DIGEST_LEN:]
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "cookie value is not valid UTF-8"


def decrypt_cookie(row: CookieRow, key: Secret, db_version: int) -> Secret:
    """Decrypt one cookie row; on failure raise ValueError(cookie name + fixed reason).

    The work happens in `_plaintext`, whose frame (holding raw key and plaintext
    bytes) has returned before this frame raises, so a traceback shows only the row
    (value excluded from its repr) and the redacted key.
    """
    value, reason = _plaintext(
        row.encrypted_value, key.reveal(), row.host_key, db_version
    )
    if value is None:
        raise ValueError(f"{row.name}: {reason}") from None
    return Secret(value)


def decrypt_store(store: CookieStore, key: Secret) -> dict[str, Secret]:
    """Decrypt every row, keyed by cookie name; the newest row wins on duplicates."""
    rows = sorted(store.rows, key=lambda r: r.creation_utc)
    return {r.name: decrypt_cookie(r, key, store.db_version) for r in rows}

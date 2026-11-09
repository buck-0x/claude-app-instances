import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from claude_instances.cookies import CookieRow, read_claude_cookies
from claude_instances.crypto import decrypt_cookie, decrypt_store, derive_key
from claude_instances.secret import Secret
from helpers import ORG_A, encrypt_cookie, make_signed_in_dir

PW = Secret("test-password")


def test_derive_key_is_16_bytes_and_deterministic():
    k = derive_key(PW).reveal()
    assert len(k) == 16 and k == derive_key(PW).reveal()


def test_roundtrip_v24_strips_host_digest():
    key = derive_key(PW)
    blob = encrypt_cookie("sk-ant-fake-session", key.reveal(), ".claude.ai", 24)
    row = CookieRow("sessionKey", ".claude.ai", blob, 0, 0)
    assert decrypt_cookie(row, key, 24).reveal() == "sk-ant-fake-session"


def test_roundtrip_pre_v24_has_no_digest():
    key = derive_key(PW)
    blob = encrypt_cookie("sk-ant-fake-session", key.reveal(), ".claude.ai", 20)
    row = CookieRow("sessionKey", ".claude.ai", blob, 0, 0)
    assert decrypt_cookie(row, key, 20).reveal() == "sk-ant-fake-session"


def test_wrong_host_digest_raises():
    key = derive_key(PW)
    blob = encrypt_cookie("sk-ant-fake-session", key.reveal(), ".example.com", 24)
    row = CookieRow("sessionKey", ".claude.ai", blob, 0, 0)
    with pytest.raises(ValueError):
        decrypt_cookie(row, key, 24)


def test_bad_prefix_raises():
    key = derive_key(PW)
    blob = encrypt_cookie("sk-ant-fake-session", key.reveal(), ".claude.ai", 24)
    row = CookieRow("sessionKey", ".claude.ai", b"v11" + blob[3:], 0, 0)
    with pytest.raises(ValueError):
        decrypt_cookie(row, key, 24)


def test_decrypt_store_keys_by_name(tmp_path):
    data_dir = make_signed_in_dir(tmp_path / "data", PW)
    store = read_claude_cookies(data_dir)
    values = decrypt_store(store, derive_key(PW))
    assert set(values) == {"sessionKey", "lastActiveOrg"}
    assert values["sessionKey"].reveal() == "sk-ant-fake-session"
    assert values["lastActiveOrg"].reveal() == ORG_A


def _unpadded_v10(key: bytes) -> bytes:
    """A v10 blob whose plaintext is one block of zeros: invalid PKCS7 padding."""
    enc = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).encryptor()
    return b"v10" + enc.update(bytes(16)) + enc.finalize()


def test_error_message_has_no_plaintext():
    key = derive_key(PW)
    good = encrypt_cookie("sk-ant-fake-session", key.reveal(), ".claude.ai", 24)
    cases = [
        ("bad prefix", ".claude.ai", b"v11" + good[3:]),
        ("bad padding", ".claude.ai", _unpadded_v10(key.reveal())),
        ("digest mismatch", ".example.com", good),
    ]
    for label, host, blob in cases:
        row = CookieRow("sessionKey", host, blob, 0, 0)
        with pytest.raises(ValueError) as exc:
            decrypt_cookie(row, key, 24)
        err = exc.value
        text = str(err) + repr(err)
        for leak in ("sk-ant-fake-session", key.reveal().hex(), blob.hex(),
                     repr(key.reveal()), repr(blob)):
            assert leak not in text, label
        assert err.__cause__ is None and err.__suppress_context__, label

import json
import os

from claude_instances.accounts import (
    AccountRow,
    ExpectedAccounts,
    expected_mismatch,
    last_known_account_uuid,
    shared_account_groups,
    short_id,
)
from claude_instances.cookies import SessionState

SIGNED_IN = SessionState.SIGNED_IN
SIGNED_OUT = SessionState.SIGNED_OUT
REJECTED = SessionState.REJECTED
UNKNOWN = SessionState.UNKNOWN

A = "11111111-2222-4333-8444-555555555555"
B = "22222222-2222-4333-8444-555555555555"


def test_last_known_account_uuid(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"lastKnownAccountUuid": A}))
    assert last_known_account_uuid(tmp_path) == A and short_id(A) == "11111111"


def test_missing_or_malformed_config_is_none(tmp_path):
    assert last_known_account_uuid(tmp_path) is None
    config = tmp_path / "config.json"
    config.write_bytes(b"{")
    assert last_known_account_uuid(tmp_path) is None
    config.write_text(json.dumps([A]))
    assert last_known_account_uuid(tmp_path) is None
    config.write_text(json.dumps({"lastKnownAccountUuid": 42}))
    assert last_known_account_uuid(tmp_path) is None
    config.write_text(json.dumps({"other": A}))
    assert last_known_account_uuid(tmp_path) is None


def test_shared_only_counts_signed_in():
    rows = [
        AccountRow("P", A, SIGNED_IN),
        AccountRow("C", A, SIGNED_IN),
        AccountRow("X", A, SIGNED_OUT),
        AccountRow("V", B, SIGNED_IN),
    ]
    assert shared_account_groups(rows) == {"P": ["C"], "C": ["P"]}
    rows = [
        AccountRow("P", A, REJECTED),
        AccountRow("C", A, SIGNED_IN),
        AccountRow("U", A, UNKNOWN),
        AccountRow("N", None, SIGNED_IN),
        AccountRow("M", None, SIGNED_IN),
    ]
    assert shared_account_groups(rows) == {"P": ["C"], "C": ["P"]}


def test_expected_mismatch():
    e = ExpectedAccounts()
    e.set("P", A)
    assert expected_mismatch(AccountRow("P", B, SIGNED_IN), e)
    assert not expected_mismatch(AccountRow("P", A, SIGNED_IN), e)
    assert not expected_mismatch(AccountRow("C", B, SIGNED_IN), e)
    assert not expected_mismatch(AccountRow("P", None, SIGNED_OUT), e)


def test_expected_accounts_roundtrip_and_mode(tmp_path):
    e = ExpectedAccounts()
    e.set("P", A)
    e.save(tmp_path / "x.json")
    assert ExpectedAccounts.load(tmp_path / "x.json").get("P") == A
    assert oct(os.stat(tmp_path / "x.json").st_mode & 0o777) == "0o600"
    assert json.loads((tmp_path / "x.json").read_text()) == {"P": A}


def test_load_missing_file_is_empty(tmp_path):
    e = ExpectedAccounts.load(tmp_path / "absent.json")
    assert e.get("P") is None

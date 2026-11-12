import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from claude_instances.api import (
    UsageLookup,
    parse_account_email,
    parse_org_uuid,
    parse_usage_response,
)
from claude_instances.crypto import derive_key
from claude_instances.httpclient import HttpResult
from claude_instances.secret import Secret
from claude_instances.usage import Source
from helpers import ORG_A, FakeHttp, encrypt_cookie, make_cookie_db, make_signed_in_dir

FIXTURES = Path(__file__).parent / "fixtures"
USAGE_FIXTURE = json.loads((FIXTURES / "usage-response.json").read_text())
ACCOUNT_FIXTURE = json.loads((FIXTURES / "account-response.json").read_text())
USAGE_FIXTURE_PCT = 37.5
WEEK_FIXTURE_PCT = 62.0
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
PW = Secret("test-password")
KEY = PW
ORG_B = "bbbbbbbb-2222-4333-8444-555555555555"
USAGE_OK = HttpResult(200, USAGE_FIXTURE, "json")


class RaisingHttp:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.called = False

    def get(self, url, cookies):
        self.called = True
        raise self.exc


# parsers

def test_parse_usage_response_from_fixture():
    u = parse_usage_response(USAGE_FIXTURE, NOW)
    assert u.source is Source.LIVE and 0 <= u.five_hour_pct <= 100
    assert u.five_hour_resets_at.tzinfo is not None
    assert u.five_hour_pct == USAGE_FIXTURE_PCT and u.week_pct == WEEK_FIXTURE_PCT
    assert u.five_hour_resets_at == datetime(2026, 11, 6, 15, 0, 0, 123456, tzinfo=timezone.utc)
    assert u.week_resets_at == datetime(2026, 11, 10, 8, 0, tzinfo=timezone.utc)
    assert u.fetched_at == NOW


def test_parse_usage_missing_keys_is_none():
    assert parse_usage_response({}, NOW) is None
    assert parse_usage_response({"seven_day_opus": None}, NOW) is None
    assert parse_usage_response([USAGE_FIXTURE], NOW) is None
    assert parse_usage_response(None, NOW) is None


def test_parse_usage_null_five_hour():
    data = copy.deepcopy(USAGE_FIXTURE)
    data["five_hour"] = None
    u = parse_usage_response(data, NOW)
    assert (u.five_hour_pct, u.five_hour_resets_at) == (None, None)
    assert u.week_pct == WEEK_FIXTURE_PCT and u.week_resets_at is not None


def test_parse_usage_only_one_window_key_present():
    u = parse_usage_response({"seven_day": {"utilization": 5, "resets_at": None}}, NOW)
    assert (u.five_hour_pct, u.week_pct, u.week_resets_at) == (None, 5, None)


def test_parse_usage_malformed_inner_values_are_none():
    data = {
        "five_hour": {"utilization": "37", "resets_at": "not a date"},
        "seven_day": {"utilization": True, "resets_at": "2026-11-10T08:00:00"},
    }
    u = parse_usage_response(data, NOW)
    assert (u.five_hour_pct, u.five_hour_resets_at) == (None, None)
    # a bool is not a number; a timestamp without a zone is rejected
    assert (u.week_pct, u.week_resets_at) == (None, None)
    u = parse_usage_response({"five_hour": "x", "seven_day": [1]}, NOW)
    assert (u.five_hour_pct, u.week_pct) == (None, None)


def test_parse_account_email():
    assert parse_account_email(ACCOUNT_FIXTURE) == "user@example.com"
    assert parse_account_email({"email_address": "no-at-sign"}) is None
    assert parse_account_email({"email_address": 5}) is None
    assert parse_account_email({}) is None
    assert parse_account_email(["user@example.com"]) is None


def test_parse_org_uuid():
    assert parse_org_uuid([{"uuid": ORG_B}, {"uuid": ORG_A}]) == ORG_B
    assert parse_org_uuid([]) is None
    assert parse_org_uuid([{"id": 1}]) is None
    assert parse_org_uuid(["x"]) is None
    assert parse_org_uuid({"uuid": ORG_A}) is None
    assert parse_org_uuid(None) is None


# lookup

def test_lookup_happy_path(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    http = FakeHttp(usage=USAGE_OK, account=HttpResult(200, ACCOUNT_FIXTURE, "json"))
    r = UsageLookup(http, KEY, now=lambda: NOW).lookup(tmp_path)
    assert r.usage.five_hour_pct == USAGE_FIXTURE_PCT and (r.rejected, r.error, r.org) == (False, None, ORG_A)
    assert r.usage.fetched_at == NOW
    assert r.email == parse_account_email(ACCOUNT_FIXTURE)
    assert f"/api/organizations/{ORG_A}/usage" in http.paths
    assert "/api/organizations" not in http.paths
    assert http.cookies[0]["sessionKey"] == Secret("sk-ant-fake-session")


def test_lookup_account_failure_still_gives_usage(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    r = UsageLookup(FakeHttp(usage=USAGE_OK), KEY, now=lambda: NOW).lookup(tmp_path)
    assert r.usage is not None and (r.error, r.email) == (None, None)


def test_lookup_without_lastactiveorg_lists_orgs(tmp_path):
    make_signed_in_dir(tmp_path, PW, org=None)
    http = FakeHttp(orgs=HttpResult(200, [{"uuid": ORG_B}, {"uuid": ORG_A}], "json"), usage=USAGE_OK)
    r = UsageLookup(http, KEY).lookup(tmp_path)
    assert http.paths[0] == "/api/organizations"
    assert http.paths[1] == f"/api/organizations/{ORG_B}/usage"
    assert r.org == ORG_B and r.usage is not None


def test_lookup_without_any_org_is_error(tmp_path):
    make_signed_in_dir(tmp_path, PW, org=None)
    http = FakeHttp(orgs=HttpResult(200, [], "json"))
    r = UsageLookup(http, KEY).lookup(tmp_path)
    assert (r.usage, r.rejected, r.error) == (None, False, "no organization")
    assert http.paths == ["/api/organizations"]


def test_lookup_orgs_rejected_is_rejected(tmp_path):
    make_signed_in_dir(tmp_path, PW, org=None)
    r = UsageLookup(FakeHttp(orgs=HttpResult(401, None, "json")), KEY).lookup(tmp_path)
    assert (r.rejected, r.error) == (True, None)


def test_html_403_is_unavailable_not_rejected(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    http = FakeHttp(usage=HttpResult(403, None, "html"))
    r = UsageLookup(http, KEY).lookup(tmp_path)
    assert f"/api/organizations/{ORG_A}/usage" in http.paths
    assert (r.rejected, r.usage, r.error) == (False, None, "live unavailable (403)")


def test_json_401_is_rejected(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    r = UsageLookup(FakeHttp(usage=HttpResult(401, {"error": "x"}, "json")), KEY).lookup(tmp_path)
    assert (r.rejected, r.error) == (True, None)


def test_json_403_is_rejected(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    r = UsageLookup(FakeHttp(usage=HttpResult(403, {"error": "x"}, "json")), KEY).lookup(tmp_path)
    assert (r.rejected, r.usage, r.error) == (True, None, None)


def test_other_status_is_unavailable(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    r = UsageLookup(FakeHttp(usage=HttpResult(500, None, "other")), KEY).lookup(tmp_path)
    assert (r.rejected, r.error) == (False, "live unavailable (500)")


def test_transport_error_is_unavailable(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    http = FakeHttp(usage=HttpResult(0, None, "error", "TimeoutError"))
    r = UsageLookup(http, KEY).lookup(tmp_path)
    assert (r.rejected, r.error) == (False, "live unavailable (TimeoutError)")


def test_unexpected_usage_shape_is_error(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    r = UsageLookup(FakeHttp(usage=HttpResult(200, {"x": 1}, "json")), KEY).lookup(tmp_path)
    assert (r.usage, r.rejected, r.error) == (None, False, "unexpected usage response")


def test_no_cookie_store_is_no_session(tmp_path):
    http = FakeHttp()
    assert UsageLookup(http, KEY).lookup(tmp_path).error == "no session cookie"
    assert http.paths == []


def test_no_session_key_is_no_session(tmp_path):
    key = derive_key(PW).reveal()
    make_cookie_db(tmp_path / "Cookies", [
        (".claude.ai", "lastActiveOrg", encrypt_cookie(ORG_A, key, ".claude.ai", 24), 0, 1)])
    http = FakeHttp()
    assert UsageLookup(http, KEY).lookup(tmp_path).error == "no session cookie"
    assert http.paths == []


def test_wrong_key_is_decrypt_error(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    assert UsageLookup(FakeHttp(), Secret("other")).lookup(tmp_path).error == "cannot decrypt cookies (wrong key?)"


def test_exception_inside_lookup_is_caught(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    http = RaisingHttp(RuntimeError("sk-ant-secret"))
    r = UsageLookup(http, KEY).lookup(tmp_path)
    assert http.called and r.error == "lookup failed (RuntimeError)"
    assert "sk-ant" not in repr(r)

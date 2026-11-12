import os

from claude_instances.cli import _build_parser
from claude_instances.httpclient import HttpResult
from claude_instances.probe import run_probe, shape
from claude_instances.secret import Secret
from helpers import FakeHttp, make_signed_in_dir, pipe_with

PW = Secret("test-password")
USER_UUID = "11111111-2222-4333-8444-555555555555"
ORG_B = "bbbbbbbb-2222-4333-8444-555555555555"


def _discard(_line: str) -> None:
    pass


def test_shape_redacts_strings_and_buckets_numbers():
    s = shape({"email_address": "a@example.com", "uuid": USER_UUID,
               "utilization": 42.5, "resets_at": "2026-11-06T12:00:00Z", "n": 0, "name": "x"})
    assert s == {"email_address": "str:email-like", "uuid": "str:uuid-like",
                 "utilization": "float:(1,100]", "resets_at": "str:iso-datetime",
                 "n": "int:0", "name": "str:len=1"}


def test_shape_scalars_and_buckets():
    assert shape(None) == "null"
    assert shape(True) == "bool"
    assert shape(1) == "int:(0,1]"
    assert shape(0.5) == "float:(0,1]"
    assert shape(101) == "int:(100,1e9]"
    assert shape(2_000_000_000) == "int:>1e9"
    assert shape(USER_UUID.upper()) == "str:uuid-like"


def test_shape_list_and_nested():
    assert shape([{"a": 1}, {"a": 2}]) == {"list": {"a": "int:(0,1]"}, "len": 2}
    assert shape([]) == {"list": None, "len": 0}


def test_shape_redacts_id_keys():
    s = shape({USER_UUID: 1, "a@example.com": 2, "1234567": 3, "five_hour": 4})
    assert set(s) == {"<uuid-key>", "<email-key>", "<digits-key>", "five_hour"}


def test_shape_redacts_uuid_prefixed_key():
    s = shape({f"{USER_UUID}:some_tool-abc123": 1, "five_hour": 2})
    assert s == {"<other-key>": "int:(0,1]", "five_hour": "int:(1,100]"}


def test_shape_redacts_key_with_spaces_and_colons():
    s = shape({"local:Made Up Server:tool": True})
    assert s == {"<other-key>": "bool"}


def test_shape_redacts_empty_key():
    assert shape({"": None}) == {"<other-key>": "null"}


def test_shape_counts_colliding_redacted_keys():
    s = shape({"a:b": 1, "c d": "x", "": None, "plain_key": 0,
               USER_UUID: 1, ORG_B: 2})
    assert s == {"<other-key> (3)": "int:(0,1]", "plain_key": "int:0",
                 "<uuid-key> (2)": "int:(0,1]"}


def test_shape_keeps_identifier_keys():
    s = shape({"_private": 1, "seven_day2": 2, "CamelCase": 3})
    assert set(s) == {"_private", "seven_day2", "CamelCase"}


def test_shape_digit_string_is_not_a_date():
    assert shape("20261106") == "str:len=8"


def test_run_probe_output_contains_no_values(tmp_path, capsys):
    make_signed_in_dir(tmp_path, PW)
    r, w = os.pipe()
    os.write(w, b"test-password")
    os.close(w)
    http = FakeHttp(account=HttpResult(200, {"email_address": "a@example.com", "uuid": USER_UUID}, "json"),
                    usage=HttpResult(200, {"five_hour": {"utilization": 12.0}}, "json"))
    assert run_probe(tmp_path, r, http, out=print) == 0
    text = capsys.readouterr().out
    assert "a@example.com" not in text and "11111111" not in text
    assert "aaaaaaaa" not in text and "sk-ant-fake" not in text
    assert "GET /api/organizations -> 404 other" in text
    assert "GET /api/account -> 200 json" in text
    assert "GET /api/organizations/{org}/usage -> 200 json" in text
    assert '"utilization": "float:(1,100]"' in text


def test_run_probe_calls_endpoints_in_order_with_cookies(tmp_path):
    http = FakeHttp()
    run_probe(make_signed_in_dir(tmp_path, PW), pipe_with(b"test-password"), http, out=_discard)
    assert http.paths == ["/api/organizations", "/api/account",
                          "/api/organizations/aaaaaaaa-2222-4333-8444-555555555555/usage"]
    assert http.cookies[0]["sessionKey"] == Secret("sk-ant-fake-session")


def test_run_probe_falls_back_to_first_org(tmp_path):
    http = FakeHttp(orgs=HttpResult(200, [{"uuid": ORG_B}, {"uuid": USER_UUID}], "json"))
    run_probe(make_signed_in_dir(tmp_path, PW, org=None), pipe_with(b"test-password"), http,
              out=_discard)
    assert http.paths[-1] == f"/api/organizations/{ORG_B}/usage"


def test_run_probe_without_any_org_skips_usage(tmp_path):
    lines: list[str] = []
    http = FakeHttp()
    rc = run_probe(make_signed_in_dir(tmp_path, PW, org=None), pipe_with(b"test-password"), http,
                   out=lines.append)
    assert rc == 0 and http.paths == ["/api/organizations", "/api/account"]
    assert lines[-1] == "GET /api/organizations/{org}/usage -> skipped: no org"


def test_run_probe_http_error_prints_kind_only(tmp_path, capsys):
    err = HttpResult(0, None, "error", "TimeoutError")
    run_probe(make_signed_in_dir(tmp_path, PW), pipe_with(b"test-password"),
              FakeHttp(orgs=err, account=err, usage=err), out=print)
    assert "GET /api/organizations -> 0 error TimeoutError" in capsys.readouterr().out


def test_run_probe_without_key_fails_with_one_line(tmp_path):
    lines: list[str] = []
    http = FakeHttp()
    rc = run_probe(make_signed_in_dir(tmp_path, PW), pipe_with(b""), http, out=lines.append)
    assert rc != 0 and len(lines) == 1 and "\n" not in lines[0]
    assert http.paths == []


def test_run_probe_non_utf8_key_fails_without_secret(tmp_path):
    lines: list[str] = []
    http = FakeHttp()
    rc = run_probe(make_signed_in_dir(tmp_path, PW), pipe_with(b"\xff\xfesecret-bytes"), http,
                   out=lines.append)
    assert rc != 0 and len(lines) == 1 and "secret-bytes" not in lines[0]
    assert http.paths == []


def test_run_probe_wrong_key_fails_without_values(tmp_path):
    lines: list[str] = []
    http = FakeHttp()
    rc = run_probe(make_signed_in_dir(tmp_path, PW), pipe_with(b"wrong-password"), http,
                   out=lines.append)
    assert rc != 0 and len(lines) == 1 and "sk-ant" not in lines[0]
    assert http.paths == []


def test_run_probe_without_cookies_db_fails(tmp_path):
    lines: list[str] = []
    http = FakeHttp()
    rc = run_probe(tmp_path, pipe_with(b"test-password"), http, out=lines.append)
    assert rc != 0 and len(lines) == 1 and http.paths == []


def test_cli_probe_key_fd_does_not_clobber_top_level():
    parser = _build_parser()
    args = parser.parse_args(["--key-fd", "5", "probe", "--key-fd", "3", "--data-dir", "/x"])
    assert args.key_fd == 5 and args.probe_key_fd == 3
    assert parser.parse_args(["--key-fd", "5"]).key_fd == 5

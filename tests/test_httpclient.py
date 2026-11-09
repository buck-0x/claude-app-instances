import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from claude_instances.httpclient import HttpResult, UrllibHttpClient
from claude_instances.secret import Secret

_ROUTES = {
    "/ok": (200, "application/json", json.dumps({"a": 1}).encode()),
    "/unauth": (401, "application/json", json.dumps({"error": "x"}).encode()),
    "/challenge": (403, "text/html; charset=utf-8", b"<html>"),
    "/boom": (500, "text/plain", b"boom"),
}


class _Server(str):
    """The server's base URL, carrying the request headers it has seen."""

    seen: list[dict[str, str]]


@pytest.fixture
def server():
    seen: list[dict[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            status, ctype, body = _ROUTES.get(self.path, (404, "text/plain", b""))
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = _Server(f"http://127.0.0.1:{httpd.server_address[1]}")
    base.seen = seen
    try:
        yield base
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_client_maps_http_errors(server):
    c = UrllibHttpClient()
    assert c.get(f"{server}/ok", {}) == HttpResult(200, {"a": 1}, "json")
    r = c.get(f"{server}/unauth", {})
    assert (r.status, r.kind, r.json) == (401, "json", {"error": "x"})
    r = c.get(f"{server}/challenge", {})
    assert (r.status, r.kind, r.json) == (403, "html", None)
    r = c.get(f"{server}/boom", {})
    assert (r.status, r.kind, r.json) == (500, "other", None)


def test_client_connection_error_has_type_only():
    r = UrllibHttpClient().get("http://127.0.0.1:9/x", {"sessionKey": Secret("sk-ant-secret")})
    assert r.kind == "error" and r.error == "URLError" and "sk-ant" not in repr(r)
    assert r.status == 0 and r.json is None


def test_client_sends_cookie_and_user_agent(server):
    UrllibHttpClient().get(
        f"{server}/ok", {"sessionKey": Secret("v"), "lastActiveOrg": Secret("o")}
    )
    headers = server.seen[-1]
    assert headers["Cookie"] == "sessionKey=v; lastActiveOrg=o"
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert headers["Accept"] == "application/json"

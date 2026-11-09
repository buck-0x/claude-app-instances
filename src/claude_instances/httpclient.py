"""Minimal cookie-authenticated GET client for the claude.ai web API.

Failures never raise and never carry an exception message (which may echo the URL
or request headers): a transport error is reported by its exception type name only.
The endpoint paths are believed correct and are confirmed by the user-run probe.
"""

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.message import Message
from typing import Any, Literal, Protocol

from claude_instances.secret import Secret

BASE_URL = "https://claude.ai"
ORGS_PATH = "/api/organizations"
USAGE_PATH = "/api/organizations/{org}/usage"
ACCOUNT_PATH = "/api/account"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT_S = 15

Kind = Literal["json", "html", "other", "error"]


@dataclass
class HttpResult:
    status: int
    json: Any | None
    kind: Kind
    error: str | None = None


class HttpClient(Protocol):
    def get(self, url: str, cookies: dict[str, Secret]) -> HttpResult: ...


def _result(status: int, headers: Message, body: bytes) -> HttpResult:
    ctype = headers.get_content_type()
    if ctype == "application/json":
        return HttpResult(status, json.loads(body), "json")
    return HttpResult(status, None, "html" if ctype == "text/html" else "other")


class UrllibHttpClient(HttpClient):
    def get(self, url: str, cookies: dict[str, Secret]) -> HttpResult:
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Cookie": "; ".join(f"{name}={value.reveal()}" for name, value in cookies.items()),
        })
        try:
            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_S) as resp:
                    return _result(resp.status, resp.headers, resp.read())
            except urllib.error.HTTPError as e:
                with e:
                    return _result(e.code, e.headers, e.read())
        except Exception as e:  # noqa: BLE001: report the type only, never the message
            return HttpResult(0, None, "error", type(e).__name__)

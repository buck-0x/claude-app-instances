# Instances TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One terminal screen that shows every Claude desktop instance's running / signed-in / account / usage state and lets the user act on an instance without hunting for its window.

**Architecture:** A Python package `src/claude_instances/` of small pure modules (discovery, process matching, cookie reading, usage sources, order file, mismatch detection) that take data in and return dataclasses out, plus two thin side-effect layers behind Protocols (`HttpClient` for claude.ai, `SystemActions` for osascript/open/kill/ps) so every flow is unit-tested with fakes. A Textual app renders one `DataTable` from a `Snapshot`; every blocking action (quit, launch all, sign-in, live lookups) runs in a thread worker. Two zsh changes (argless-default rule, `--launch-all`) are made in `bin/new-claude.sh` with end-to-end tests against a stub Claude.app, in the style of `tests/test-watchdog.sh`.

**Tech Stack:** Python >= 3.11, Textual 8.x, cryptography, stdlib `sqlite3` / `plistlib` / `urllib`; pytest + pytest-asyncio; zsh for `bin/new-claude.sh` and its tests.

**Spec:** `docs/superpowers/specs/2026-11-03-instances-tui-design.md`

## Global Constraints

- `pyproject.toml` pins `requires-python = ">=3.11"`. Nothing in the repo references mise; README documents `pipx install .` and `pip install -e '.[dev]'`.
- No personal data (emails, real account/org UUIDs) in any committed file, fixture or test output. Fixtures use made-up IDs such as `11111111-2222-4333-8444-555555555555`; placeholder addresses use the `example.com` domain only.
- Prose in files this plan creates uses colons or semicolons, never em dashes. The table's no-data glyph is the constant `NO_DATA = "\N{EM DASH}"` in code, never typed as a literal.
- The TUI never calls `open -a` on Claude.app, never starts Claude directly, and launches instances only with `open -n "<launcher>.app"`, always through `guarded_launch` (Task 13), which re-reads `ps` immediately before opening.
- Secrets (Safe Storage key, decrypted cookies, raw API responses) live only in memory inside `Secret` wrappers; never in files, logs, stdout, exceptions or test output. The repo is public.
- Every usage number carries its age; a number is shown as live only while it is from a live lookup no older than `LIVE_STALE_AFTER_S`; otherwise it is marked cached with its age.
- All reads of Claude data dirs are read-only: SQLite via a percent-encoded `file:...?immutable=1` URI, JSON read without writing back.
- `new-claude`, `remove-claude` and the generated launcher keep their existing behavior except the argless-default rule in `instance_running_for()` / `instance_pid()` and the new `--launch-all` mode. `new-claude --list` output does not change. `tests/test-watchdog.sh` must still pass unchanged.
- Constants: `WARN_PCT = 80`, `CRIT_PCT = 95` (usage.py); `LIVE_INTERVAL_S = 300`, `LIVE_STALE_AFTER_S = 360` (usage.py); `STATE_INTERVAL_S = 10` (scheduler.py); `LAUNCH_GAP_S = 3`, `QUIT_TIMEOUT_S = 30`, `WATCHDOG_STOP_TIMEOUT_S = 5` (actions.py); `LAUNCH_TIMEOUT_S = 60` (signin.py).
- File locations: order `~/.config/claude-instances/order.txt`, expected accounts `~/.config/claude-instances/expected-accounts.json`, usage cache `~/.config/claude-instances/usage-cache.json`, restore list `~/.config/claude-instances/signin-restore.txt`, watchdog logs/locks `~/Library/Logs/claude-instances/<name>.log|.lock`. The config dir is overridable with env `CLAUDE_INSTANCES_CONFIG_DIR` (read by both Python and zsh); the default data folder `~/Library/Application Support/Claude` is overridable with env `CLAUDE_LAUNCHER_DEFAULT_DATA_DIR` (test hook, read by both; `new-claude` bakes the resolved value into each launcher).
- Unlisted launchers sort by plain codepoint order in both Python (`sorted()`) and zsh (`LC_ALL=C sort`), so the TUI and the Dock app agree.
- User-in-the-loop checkpoints (Tasks 5 and 22): the executing agent stops and hands the step to the user. The agent never reads the Keychain, never reads or decrypts a real Cookies database, never calls claude.ai or any other network service, and never launches, quits or signals a real Claude process or watchdog. The one network exception is `pip install` of the dependencies declared in `pyproject.toml` (Task 1, and any reinstall after a dependency change in Task 22) from PyPI; no other lookups or downloads. Unit tests use `tmp_path` fixtures and a localhost test server only.

## Review Focus

Spec-implied inputs no task's happy path exercises, most likely to bite first; each has its pinning test in the owning task.

1. claude.ai answers 403 with an HTML Cloudflare challenge (urllib raises `HTTPError`): the row must say live is unavailable and keep showing cached numbers, not flip to "signed out" or "session rejected" (Task 5, `test_client_maps_http_errors`; Task 10, `test_html_403_is_unavailable_not_rejected`).
2. A launcher built before this work (no argless rule, or an old one-shot launcher) is re-armed or launched while an argless default Claude runs: the TUI must refuse instead of starting a second instance on the default folder (Task 13, `test_guarded_launch_refuses_*`).
3. A data dir with no `Cookies` file yet, or a `Cookies` file mid-commit: the state is "unknown", the TUI keeps running (Task 3, `test_missing_db_is_unknown`, `test_database_error_retries_then_unknown`).
4. The user exits the TUI (ctrl+q, ctrl+c, terminal SIGHUP/SIGTERM) mid sign-in: everything quit must be relaunched before exit, and no action key may launch anything while the sign-in runs (Task 17, `test_exit_during_signin_restores_first`, `test_builtin_quit_routes_through_request_exit`, `test_action_keys_blocked_during_signin`).
5. Terminal without Automation permission for System Events: bring-to-front shows the one-line grant instruction instead of a raw osascript error (Task 13, `test_automation_denied_detected`; Task 17, `test_front_denied_shows_help`).

---

### Task 1: Package scaffold and paths

**Files:**
- Create: `src/claude_instances/__init__.py`, `pyproject.toml`, `src/claude_instances/paths.py`, `src/claude_instances/cli.py`, `tests/test_paths.py`, `tests/conftest.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `paths.config_dir() -> Path`, `paths.default_data_dir() -> Path`, `paths.support_dir() -> Path` (`~/Library/Application Support`), `paths.log_dir() -> Path`, `paths.apps_dirs() -> list[Path]` (`/Applications` and `~/Applications`), constants `ORDER_FILE = "order.txt"`, `EXPECTED_FILE = "expected-accounts.json"`, `USAGE_CACHE_FILE = "usage-cache.json"`, `RESTORE_FILE = "signin-restore.txt"`; `cli.main(argv: list[str] | None = None) -> int` with argparse: `--key-fd INT`, `--apps PATH` (repeatable; replaces the default scan dirs, same meaning as `new-claude --apps`), subcommand `probe` (filled in Task 5).

- [ ] **Step 1: Create `src/claude_instances/__init__.py`** (`__version__ = "0.1.0"`) so the editable install below has a package to find.

- [ ] **Step 2: Write `pyproject.toml`**

Keys: `[build-system]` setuptools>=68; `[project]` name `claude-instances`, version `0.1.0`, `requires-python = ">=3.11"`, `dependencies = ["textual>=8.2,<9", "cryptography>=42"]`; `[project.optional-dependencies] dev = ["pytest>=8", "pytest-asyncio>=0.23"]`; `[project.scripts] claude-instances = "claude_instances.cli:main"`; `[tool.setuptools.packages.find] where = ["src"]`; `[tool.pytest.ini_options] testpaths = ["tests"]`, `asyncio_mode = "auto"`.

- [ ] **Step 3: Add to `.gitignore`**

`.venv/`, `__pycache__/`, `*.egg-info/`, `dist/`, `build/`, `.pytest_cache/`.

- [ ] **Step 4: Write the failing test**

`tests/conftest.py` puts `tests/` on `sys.path` so tests can `from helpers import ...`.

```python
# tests/test_paths.py
def test_config_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_INSTANCES_CONFIG_DIR", str(tmp_path))
    assert paths.config_dir() == tmp_path

def test_default_data_dir_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_LAUNCHER_DEFAULT_DATA_DIR", str(tmp_path))
    assert paths.default_data_dir() == tmp_path

def test_defaults(monkeypatch):
    monkeypatch.delenv("CLAUDE_INSTANCES_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_LAUNCHER_DEFAULT_DATA_DIR", raising=False)
    assert paths.config_dir() == Path.home() / ".config/claude-instances"
    assert paths.default_data_dir() == Path.home() / "Library/Application Support/Claude"
    assert paths.apps_dirs() == [Path("/Applications"), Path.home() / "Applications"]
```

- [ ] **Step 5: Create the venv and run the test to verify it fails**

Run: `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`
Run: `.venv/bin/pytest tests/test_paths.py -v`
Expected: FAIL, `ModuleNotFoundError: claude_instances.paths`

- [ ] **Step 6: Implement `paths.py` and the `cli.py` skeleton**

`cli.main` parses args and, with no subcommand, prints "TUI not wired yet" and returns 0 (replaced in Task 18). The `probe` subparser takes `--key-fd INT` (required) and `--data-dir PATH` (required) and returns 2 with "not implemented" until Task 5.

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/pytest -v`
Expected: 3 passed

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .gitignore src/claude_instances tests/test_paths.py tests/conftest.py
git commit -m "feat(tui): package scaffold, paths and CLI skeleton"
```

---

### Task 2: Redacting secret wrapper and key delivery over a pipe

**Files:**
- Create: `src/claude_instances/secret.py`, `tests/test_secret.py`

**Interfaces:**
- Produces: `class Secret` (holds `str | bytes`; `reveal() -> str | bytes`; `__repr__` and `__str__` both return `"Secret(<redacted>)"`; `__eq__` compares revealed values; `__len__`); `read_secret_fd(fd: int) -> Secret | None` (reads fd to EOF, strips one trailing newline, closes fd; returns None if fd is not open or is empty).

- [ ] **Step 1: Write the failing tests**

```python
def test_repr_and_str_redact():
    s = Secret("hunter2")
    assert repr(s) == "Secret(<redacted>)" and str(s) == "Secret(<redacted>)"
    assert "hunter2" not in f"{s!r}{s}"
    assert s.reveal() == "hunter2"

def test_secret_not_in_format_of_container():
    assert "hunter2" not in repr({"k": Secret("hunter2")})

def test_read_secret_fd_from_pipe():
    r, w = os.pipe(); os.write(w, b"pw\n"); os.close(w)
    assert read_secret_fd(r) == Secret("pw")

def test_read_secret_fd_closed_fd_is_none():
    fd = resource.getrlimit(resource.RLIMIT_NOFILE)[0] - 1          # far above anything pytest opens
    with pytest.raises(OSError): fcntl.fcntl(fd, fcntl.F_GETFD)       # proves it is not open
    assert read_secret_fd(fd) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_secret.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `secret.py`**

`read_secret_fd` guards with `os.fstat(fd)` in a try/except `OSError`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_secret.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/secret.py tests/test_secret.py
git commit -m "feat(tui): redacting Secret wrapper and fd key delivery"
```

---

### Task 3: Cookie store reading and signed-in state

**Files:**
- Create: `src/claude_instances/cookies.py`, `tests/helpers.py`, `tests/test_cookies.py`

**Interfaces:**
- Produces: `@dataclass CookieRow(name: str, host_key: str, encrypted_value: bytes, expires_utc: int, creation_utc: int)`; `@dataclass CookieStore(rows: list[CookieRow], db_version: int)`; `read_claude_cookies(data_dir: Path, retries: int = 3, sleep=time.sleep) -> CookieStore | None` (reads `<data_dir>/Cookies`, falling back to `<data_dir>/Network/Cookies`; opens `"file:" + urllib.parse.quote(str(path)) + "?immutable=1"` with `uri=True`; selects `name, host_key, encrypted_value, expires_utc, creation_utc` where `host_key` ends with `claude.ai` and `name IN ('sessionKey','lastActiveOrg')`; `db_version` from `meta` table key `version`; returns None when no file exists or every retry raised `sqlite3.DatabaseError`); `chromium_time(us: int) -> datetime | None` (None for 0; else `datetime(1601,1,1,tzinfo=UTC) + timedelta(microseconds=us)`); `class SessionState(Enum): SIGNED_IN, SIGNED_OUT, UNKNOWN, REJECTED`; `session_state(store: CookieStore | None, now: datetime) -> tuple[SessionState, datetime | None]` (expiry returned for SIGNED_IN; `expires_utc == 0` is SIGNED_IN with expiry None); `session_created(store: CookieStore | None) -> int | None` (largest `creation_utc` among `sessionKey` rows; used to notice a fresh sign-in).
- `tests/helpers.py`: `make_cookie_db(path: Path, rows: list[tuple[str, str, bytes, int, int]], version: int = 24) -> None` creating tables `cookies(host_key, name, encrypted_value, expires_utc, creation_utc)` and `meta(key, value)`; `chromium_us(dt: datetime) -> int` inverse of `chromium_time`.

- [ ] **Step 1: Write the failing tests**

```python
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)

def test_signed_in_with_future_expiry(tmp_path):
    make_cookie_db(tmp_path / "Cookies", [(".claude.ai", "sessionKey", b"v10xx", chromium_us(NOW + timedelta(days=30)), chromium_us(NOW))])
    store = read_claude_cookies(tmp_path)
    state, exp = session_state(store, NOW)
    assert state is SessionState.SIGNED_IN and exp == NOW + timedelta(days=30)
    assert session_created(store) == chromium_us(NOW)

def test_session_cookie_zero_expiry_is_signed_in(tmp_path): ...  # expires_utc=0 -> (SIGNED_IN, None)
def test_expired_is_signed_out(tmp_path): ...                   # expiry NOW - 1 day -> SIGNED_OUT
def test_no_session_cookie_is_signed_out(tmp_path): ...         # only lastActiveOrg row
def test_other_host_ignored(tmp_path): ...                      # sessionKey on ".example.com" -> SIGNED_OUT
def test_missing_db_is_unknown(tmp_path):
    assert read_claude_cookies(tmp_path) is None
    assert session_state(None, NOW) == (SessionState.UNKNOWN, None)
def test_network_fallback(tmp_path): ...                        # only Network/Cookies exists -> read
def test_path_with_space_and_hash(tmp_path):                    # data dir "Application Support/#x" opens fine
    d = tmp_path / "Application Support" / "#x"; d.mkdir(parents=True)
    make_cookie_db(d / "Cookies", [(".claude.ai", "sessionKey", b"v10xx", 0, 1)])
    assert read_claude_cookies(d) is not None
def test_database_error_retries_then_unknown(tmp_path):
    (tmp_path / "Cookies").write_bytes(b"not a database")
    calls = []
    assert read_claude_cookies(tmp_path, retries=3, sleep=calls.append) is None
    assert len(calls) == 2
def test_chromium_time():
    assert chromium_time(0) is None
    assert chromium_time(11644473600_000_000) == datetime(1970, 1, 1, tzinfo=timezone.utc)
def test_db_version_read(tmp_path): ...                         # version=24 -> store.db_version == 24
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cookies.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `cookies.py` and `tests/helpers.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cookies.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/cookies.py tests/helpers.py tests/test_cookies.py
git commit -m "feat(tui): read-only cookie store and signed-in state"
```

---

### Task 4: Cookie decryption

**Files:**
- Create: `src/claude_instances/crypto.py`, `tests/test_crypto.py`
- Modify: `tests/helpers.py`

**Interfaces:**
- Consumes: `Secret` (Task 2), `CookieRow`/`CookieStore` (Task 3).
- Produces: `derive_key(password: Secret) -> Secret` (PBKDF2-HMAC-SHA1, salt `b"saltysalt"`, 1003 iterations, 16 bytes; `hashlib.pbkdf2_hmac`); `decrypt_cookie(row: CookieRow, key: Secret, db_version: int) -> Secret` (requires `b"v10"` prefix; AES-128-CBC, IV 16 spaces, PKCS7 unpad; when `db_version >= 24` strips the 32-byte leading `sha256(row.host_key)` digest; raises `ValueError` on bad prefix, bad padding or digest mismatch, with a fixed message that contains no key, blob or plaintext, using `raise ValueError(...) from None` so no chained exception carries them); `decrypt_store(store: CookieStore, key: Secret) -> dict[str, Secret]` keyed by cookie name.
- `tests/helpers.py` gains `encrypt_cookie(plaintext: str, key: bytes, host_key: str, db_version: int) -> bytes` (the inverse, used only by tests) and `make_signed_in_dir(root: Path, password: Secret, org: str | None = ORG_A, session: str = "sk-ant-fake-session", created: int = 1) -> Path` (a data dir whose `Cookies` holds encrypted `sessionKey` and, when `org` is given, `lastActiveOrg` rows; reused by Tasks 5 and 10). `ORG_A = "aaaaaaaa-2222-4333-8444-555555555555"` lives in helpers.

- [ ] **Step 1: Write the failing tests**

```python
PW = Secret("test-password")

def test_derive_key_is_16_bytes_and_deterministic():
    k = derive_key(PW).reveal()
    assert len(k) == 16 and k == derive_key(PW).reveal()

def test_roundtrip_v24_strips_host_digest():
    key = derive_key(PW)
    blob = encrypt_cookie("sk-ant-fake-session", key.reveal(), ".claude.ai", 24)
    row = CookieRow("sessionKey", ".claude.ai", blob, 0, 0)
    assert decrypt_cookie(row, key, 24).reveal() == "sk-ant-fake-session"

def test_roundtrip_pre_v24_has_no_digest(): ...   # db_version=20 blob decrypts without stripping
def test_wrong_host_digest_raises(): ...          # encrypted for ".example.com", row host ".claude.ai" -> ValueError
def test_bad_prefix_raises(): ...                 # b"v11..." -> ValueError
def test_decrypt_store_keys_by_name(tmp_path):    # make_signed_in_dir -> {"sessionKey", "lastActiveOrg"}
def test_error_message_has_no_plaintext():        # str(ValueError) contains neither key bytes nor blob
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_crypto.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `crypto.py` and the helpers**

Use `cryptography.hazmat.primitives.ciphers` (AES/CBC) and `cryptography.hazmat.primitives.padding.PKCS7`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_crypto.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/crypto.py tests/helpers.py tests/test_crypto.py
git commit -m "feat(tui): Chromium cookie decryption with v24 host digest"
```

---

### Task 5: HTTP layer, redacting probe, and CHECKPOINT A (user runs the probe and a graceful-quit check)

**Files:**
- Create: `src/claude_instances/httpclient.py`, `src/claude_instances/probe.py`, `tests/test_httpclient.py`, `tests/test_probe.py`
- Modify: `src/claude_instances/cli.py` (wire the `probe` subcommand)

**Interfaces:**
- Consumes: `Secret`, `read_secret_fd` (Task 2); `read_claude_cookies` (Task 3); `derive_key`, `decrypt_store`, `make_signed_in_dir` (Task 4).
- Produces: `@dataclass HttpResult(status: int, json: Any | None, kind: Literal["json", "html", "other", "error"], error: str | None = None)`; `class HttpClient(Protocol): def get(self, url: str, cookies: dict[str, Secret]) -> HttpResult`; `class UrllibHttpClient(HttpClient)` sending `User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36`, `Accept: application/json`, `Cookie: name=value; ...`, 15 s timeout. Response handling: on success, `kind` from `resp.headers.get_content_type()` (`application/json` -> parse to `json`, `text/html` -> `"html"`, else `"other"`); `except urllib.error.HTTPError as e` first: `status=e.code`, kind from `e.headers.get_content_type()`, JSON body parsed when the type is JSON (body discarded otherwise); `except Exception as e` last: `status=0, kind="error", error=type(e).__name__` only (never the message, which may echo the URL or headers). Constants `BASE_URL = "https://claude.ai"`, `ORGS_PATH = "/api/organizations"`, `USAGE_PATH = "/api/organizations/{org}/usage"`, `ACCOUNT_PATH = "/api/account"` (all "believed", confirmed by this task's checkpoint).
- `probe.shape(value: Any) -> Any`: None -> `"null"`; bool -> `"bool"`; int/float -> `"int:<bucket>"` / `"float:<bucket>"` with bucket in `0`, `(0,1]`, `(1,100]`, `(100,1e9]`, `>1e9`; str -> `"str:iso-datetime"` if it contains `-` or `:` and `datetime.fromisoformat(s.replace("Z", "+00:00"))` parses (so bare digit strings such as `"20261106"` are not dates), `"str:uuid-like"` if it matches `^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$` (case-insensitive), `"str:email-like"` if it contains `@`, else `"str:len=<n>"`; list -> `{"list": shape(first) if non-empty else None, "len": n}`; dict -> `{redact_key(k): shape(v)}` where `redact_key` returns `"<uuid-key>"` for uuid-like keys, `"<email-key>"` for keys containing `@`, `"<digits-key>"` for keys of 6 or more digits, else the key (field names are kept; ids used as keys are not).
- `probe.run_probe(data_dir: Path, key_fd: int, http: HttpClient, out=print) -> int`: reads the key from the fd, decrypts `sessionKey` and `lastActiveOrg`, calls `ORGS_PATH`, `ACCOUNT_PATH`, then `USAGE_PATH` for `lastActiveOrg` (or the first org uuid from the orgs response); for each prints exactly `f"GET {path_template} -> {status} {kind}" + (f" {error}" if error else "")` (`{org}` stays literal) followed by `json.dumps(shape(json), indent=1)` when `json` is not None; prints nothing else; returns 0.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_httpclient.py: a ThreadingHTTPServer on 127.0.0.1:0 in a daemon thread, routes:
#   /ok -> 200 application/json {"a": 1}; /unauth -> 401 application/json {"error": "x"};
#   /challenge -> 403 text/html "<html>"; /boom -> 500 text/plain
def test_client_maps_http_errors(server):
    c = UrllibHttpClient()
    assert c.get(f"{server}/ok", {}) == HttpResult(200, {"a": 1}, "json")
    r = c.get(f"{server}/unauth", {}); assert (r.status, r.kind) == (401, "json")
    r = c.get(f"{server}/challenge", {}); assert (r.status, r.kind, r.json) == (403, "html", None)
    r = c.get(f"{server}/boom", {}); assert (r.status, r.kind) == (500, "other")
def test_client_connection_error_has_type_only():
    r = UrllibHttpClient().get("http://127.0.0.1:9/x", {"sessionKey": Secret("sk-ant-secret")})
    assert r.kind == "error" and r.error == "URLError" and "sk-ant" not in repr(r)
def test_client_sends_cookie_and_user_agent(server): ...   # server records headers: Cookie "sessionKey=v", UA starts "Mozilla/5.0"

# tests/test_probe.py
def test_shape_redacts_strings_and_buckets_numbers():
    s = shape({"email_address": "a@example.com", "uuid": "11111111-2222-4333-8444-555555555555",
               "utilization": 42.5, "resets_at": "2026-11-06T12:00:00Z", "n": 0, "name": "x"})
    assert s == {"email_address": "str:email-like", "uuid": "str:uuid-like",
                 "utilization": "float:(1,100]", "resets_at": "str:iso-datetime", "n": "int:0", "name": "str:len=1"}
def test_shape_list_and_nested():
    assert shape([{"a": 1}, {"a": 2}]) == {"list": {"a": "int:(0,1]"}, "len": 2}
    assert shape([]) == {"list": None, "len": 0}
def test_shape_redacts_id_keys():
    s = shape({"11111111-2222-4333-8444-555555555555": 1, "a@example.com": 2, "1234567": 3, "five_hour": 4})
    assert set(s) == {"<uuid-key>", "<email-key>", "<digits-key>", "five_hour"}
def test_run_probe_output_contains_no_values(tmp_path, capsys):
    make_signed_in_dir(tmp_path, PW); r, w = os.pipe(); os.write(w, b"test-password"); os.close(w)
    http = FakeHttp(account=HttpResult(200, {"email_address": "a@example.com", "uuid": "11111111-2222-4333-8444-555555555555"}, "json"),
                    usage=HttpResult(200, {"five_hour": {"utilization": 12.0}}, "json"))
    run_probe(tmp_path, r, http, out=print)
    text = capsys.readouterr().out
    assert "a@example.com" not in text and "11111111" not in text and "aaaaaaaa" not in text and "sk-ant-fake" not in text
    assert "GET /api/organizations/{org}/usage -> 200 json" in text
def test_run_probe_http_error_prints_kind_only(tmp_path, capsys):
    err = HttpResult(0, None, "error", "TimeoutError")
    run_probe(make_signed_in_dir(tmp_path, PW), pipe_with(b"test-password"), FakeHttp(orgs=err, account=err, usage=err), out=print)
    assert "GET /api/organizations -> 0 error TimeoutError" in capsys.readouterr().out
def test_shape_digit_string_is_not_a_date():
    assert shape("20261106") == "str:len=8"
```

`tests/helpers.py` gains `FakeHttp(orgs: HttpResult | None = None, account: HttpResult | None = None, usage: HttpResult | None = None)`: `get(url, cookies)` strips the scheme and host, appends the path to `paths: list[str]`, and returns `usage` for paths ending `/usage`, `account` for `ACCOUNT_PATH`, `orgs` for `ORGS_PATH`, or `HttpResult(404, None, "other")` when the matching argument is None. It also gains `pipe_with(data: bytes) -> int` (a pipe's read end with `data` written and the write end closed). Both are reused in Task 10.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_httpclient.py tests/test_probe.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `httpclient.py`, `probe.py`, and wire `cli.py probe`**

`cli probe --key-fd N --data-dir PATH` calls `run_probe(Path(args.data_dir), args.key_fd, UrllibHttpClient())`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_httpclient.py tests/test_probe.py -v`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/httpclient.py src/claude_instances/probe.py src/claude_instances/cli.py tests/test_httpclient.py tests/test_probe.py tests/helpers.py
git commit -m "feat(tui): HTTP client and shape-only usage API probe"
```

- [ ] **Step 6: CHECKPOINT A: stop and hand the probe and the graceful-quit check to the user**

Before posting, the agent re-reads `av --help`, `av save --help`, `av inject --help` and `av bless --help` (local help text only) and corrects the commands below if the syntax changed. Apart from those help calls, the executing agent does not run any of these commands. Post this to the user and wait:

1. Store the Safe Storage password in Automic Vault once (the password is the Keychain item service "Claude Safe Storage", account "Claude Key"; this pipes it straight into av without echoing it):
   `security find-generic-password -s "Claude Safe Storage" -a "Claude Key" -w | av save --stdin CLAUDE_SAFE_STORAGE`
2. Install the package and bless the entry point:
   `pipx install --editable .` then `av bless ~/.local/bin/claude-instances` (if av refuses because the path is a launcher or symlink, use `av bless --endorse-launcher ~/.local/bin/claude-instances`; tell the agent which form worked so the README can say so).
3. Run the probe against one signed-in instance's data dir:
   `av inject --mode=fd +CLAUDE_SAFE_STORAGE:3 -- claude-instances probe --key-fd 3 --data-dir "$HOME/Library/Application Support/<data folder>"`
4. Paste the probe output back. It contains only paths, statuses and the shape (keys, types, buckets): no emails, IDs, cookies or raw bodies. If any line looks like a real value, do not paste it; report the line position instead.
5. Graceful-quit check, same session, on one idle instance that is not the default folder (pick `<Name>` and its `<data folder>` from `new-claude --list`):
   - stop its watchdog: `pkill -TERM -f "/<Name>.app/Contents/MacOS/launcher"`, then confirm `pgrep -f "/<Name>.app/Contents/MacOS/launcher"` prints nothing (repeat after a few seconds if it does);
   - find its main pid: `ps -axo pid=,command= | grep -F -- "--user-data-dir=$HOME/Library/Application Support/<data folder>" | grep -v -e --type= -e grep`;
   - `kill -TERM <pid>`; within 30 s the process should be gone;
   - `tail -n 60 ~/Library/Logs/Claude/main.log | grep -i -c -E 'beforequit|willquit|before-quit|will-quit'` and report only the count (not the lines);
   - relaunch with `open -n "/Applications/<Name>.app"`.
   Report: did it exit within 30 s, and was the count above 0.
6. Cookie journal mode: `ls "$HOME/Library/Application Support/<data folder>" | grep -c -E '^Cookies-(wal|shm)$'` and report only the count. A non-zero count means the database uses WAL, which `immutable=1` ignores, so a fresh sign-in may not show until Chromium checkpoints. In that case the agent raises it with the user before Task 16. The proposed change is to open with `mode=ro` instead of `immutable=1`, after a stub test shows `mode=ro` writes nothing into the folder.

The agent resumes with the pasted shape, the quit result and the journal count. Task 10 depends on the shape; Task 13's `QUIT_SIGNAL` depends on the quit result (SIGTERM if clean; otherwise the Task 13 fallback). Tasks 6 to 9, 11 to 17 (except the `QUIT_SIGNAL` value) do not depend on the checkpoint and may proceed while waiting; Task 18 needs Task 10. If the usage endpoint answered something other than 200 json, the agent proposes one alternative path at a time for the user to re-run; it never calls the API itself.

---

### Task 6: Launcher discovery

**Files:**
- Create: `src/claude_instances/launchers.py`, `tests/test_launchers.py`
- Modify: `tests/helpers.py` (add `make_launcher_app`)

**Interfaces:**
- Produces: `@dataclass(frozen=True) Launcher(name: str, app_path: Path, script_path: Path, data_dir: Path, claude_app: Path, is_watchdog: bool, has_argless_rule: bool)` (`is_watchdog`: the script has a `DATA_DIR="..."` header line, so it holds the per-instance lock; `has_argless_rule`: the script has a `DEFAULT_DATA_DIR="..."` header line, added by Task 19); `parse_launcher_script(text: str, default_claude_app: Path) -> tuple[Path | None, Path, bool, bool]` (data dir from `DATA_DIR="..."`, else from inline `--user-data-dir="..."`; claude app from `CLAUDE_APP="..."`, else from `open -n -a "..."`, else default; then the two flags); `discover_launchers(apps_dirs: list[Path], default_claude_app: Path = Path("/Applications/Claude.app")) -> list[Launcher]` (every `*.app` whose Info.plist `CFBundleIdentifier` starts with `local.launcher.`; name from `CFBundleDisplayName`; unreadable plist or no data dir -> skipped; sorted by name); `rebuild_command(launcher: Launcher, support_dir: Path) -> str | None` (`new-claude "<name>" --dir "<data_dir.name>"` plus ` --apps "<app_path.parent>"` when the parent is not `/Applications`, shell-quoted with `shlex.quote`; None when `data_dir.parent != support_dir`, because `new-claude` can only build data dirs under Application Support).
- `tests/helpers.py`: `make_launcher_app(apps_dir: Path, name: str, data_dir: Path, bundle_id: str | None = None, fmt: Literal["argless", "watchdog", "oneshot"] = "argless", claude_app: Path = Path("/Applications/Claude.app"), default_data_dir: Path | None = None) -> Path` writing `Info.plist` via `plistlib.dump` and `Contents/MacOS/launcher` with the header lines for the chosen format (`argless`: `CLAUDE_APP=`, `DATA_DIR=`, `DEFAULT_DATA_DIR=`, `INSTANCE_NAME=`; `watchdog`: same without `DEFAULT_DATA_DIR=`; `oneshot`: the single `exec open -n -a "..." --args --user-data-dir="..."` line).

- [ ] **Step 1: Write the failing tests**

```python
def test_discovers_all_formats(tmp_path):
    make_launcher_app(tmp_path, "Claude Alpha", tmp_path / "AS/Claude-Alpha")
    make_launcher_app(tmp_path, "Claude Mid", tmp_path / "AS/Claude-Mid", fmt="watchdog")
    make_launcher_app(tmp_path, "Claude Old", tmp_path / "AS/Claude-Old", fmt="oneshot")
    a, m, o = discover_launchers([tmp_path])
    assert (a.is_watchdog, a.has_argless_rule) == (True, True)
    assert (m.is_watchdog, m.has_argless_rule) == (True, False)
    assert (o.is_watchdog, o.has_argless_rule, o.data_dir) == (False, False, tmp_path / "AS/Claude-Old")
    assert a.script_path == tmp_path / "Claude Alpha.app/Contents/MacOS/launcher"

def test_ignores_chatgpt_and_foreign_apps(tmp_path):
    make_launcher_app(tmp_path, "ChatGPT X", tmp_path / "d", bundle_id="local.launcher-chatgpt.x")
    make_launcher_app(tmp_path, "Other", tmp_path / "d", bundle_id="com.example.other")
    assert discover_launchers([tmp_path]) == []

def test_rebuild_command_keeps_data_folder_and_apps_dir(tmp_path):
    support = tmp_path / "AS"
    l = make_and_discover(tmp_path / "Apps", "Claude Personal", support / "Claude")
    assert rebuild_command(l, support) == f"new-claude 'Claude Personal' --dir Claude --apps {tmp_path / 'Apps'}"
    assert rebuild_command(replace(l, data_dir=tmp_path / "elsewhere/X"), support) is None

def test_data_dir_with_spaces(tmp_path): ...            # "Application Support" in path parsed intact
def test_unreadable_plist_skipped(tmp_path): ...         # Info.plist is b"garbage" -> skipped, no exception
def test_claude_app_override_line(tmp_path): ...         # CLAUDE_APP="/tmp/FakeClaude.app" -> launcher.claude_app
def test_scans_multiple_dirs_and_missing_dir(tmp_path): # [tmp_path/"a", tmp_path/"none"] -> only a's launchers
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_launchers.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `launchers.py` and `make_launcher_app`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_launchers.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/launchers.py tests/helpers.py tests/test_launchers.py
git commit -m "feat(tui): discover launcher apps by bundle id"
```

---

### Task 7: Running-process matching and watchdog status

**Files:**
- Create: `src/claude_instances/processes.py`, `tests/test_processes.py`, `tests/fixtures/ps-output.txt`

**Interfaces:**
- Consumes: `Launcher` (Task 6).
- Produces: `@dataclass(frozen=True) ProcessLine(pid: int, command: str)`; `parse_ps(text: str) -> list[ProcessLine]` (split each line at the first whitespace run after the pid; never splits the command); `list_processes() -> list[ProcessLine]` (runs `ps -axo pid=,command=`); `claude_binary(claude_app: Path) -> str` (`<app>/Contents/MacOS/<CFBundleExecutable or "Claude">`); `@dataclass(frozen=True) MainProcess(pid: int, data_dir: Path | None)` (`None` = argless); `main_processes(lines: list[ProcessLine], binary: str) -> list[MainProcess]`: a line is a main process when `binary` appears at the start of the command or after a space, and excludes any line containing `--type=`; it has a data dir when it contains `--user-data-dir=` (value up to end or `" --"`); it is argless only when it has no `--user-data-dir=` and the text right after `binary` is empty or starts with `" -"` (the binary run with no arguments, or flags only; a line like `<binary> /path/script.js --foo` is neither); `@dataclass Running(pid: int, argless: bool)`; `match_instances(procs: list[MainProcess], launchers: list[Launcher], default_data_dir: Path) -> tuple[dict[str, list[Running]], list[MainProcess]]` (per launcher name the matches, argless procs assigned to the launcher whose `data_dir == default_data_dir`; second element: main processes matching no launcher, including argless ones when no default launcher exists); `watchdog_pids(lines: list[ProcessLine], script_path: Path) -> list[int]` (command contains `str(script_path)`); `watchdog_alive(name: str, script_path: Path, lines: list[ProcessLine], log_dir: Path) -> bool` (lock `<log_dir>/<name>.lock` is a symlink whose target pid is in `watchdog_pids`).

- [ ] **Step 1: Write the fixture and failing tests**

`tests/fixtures/ps-output.txt` (made-up user `u`): pids 501 main for `Claude-V1`, 502 main for `Claude-V12`, 503 `--type=utility` helper carrying the main binary path and `--user-data-dir=.../Claude-V1`, 504 argless main (binary alone), 505 and 506 two mains for `Claude-Dup`, 507 a renderer helper from `Contents/Frameworks/Claude Helper (Renderer).app/...`, 508 `/bin/zsh /Apps/Claude V1.app/Contents/MacOS/launcher`, 509 an unrelated `/usr/bin/grep Claude` line, 510 `<binary> /tmp/script.js --foo`, 511 `<binary> -psn_0_12345` (argless with a flag).

```python
BIN = "/Applications/Claude.app/Contents/MacOS/Claude"
AS = Path("/Users/u/Library/Application Support")

def test_parse_ps_keeps_spaces_in_command():
    lines = parse_ps(FIXTURE)
    assert lines[0] == ProcessLine(501, f"{BIN} --user-data-dir={AS}/Claude-V1")

def test_main_processes_excludes_type_helpers_grep_and_non_flag_args():
    procs = main_processes(parse_ps(FIXTURE), BIN)
    assert sorted(p.pid for p in procs) == [501, 502, 504, 505, 506, 511]
    assert MainProcess(504, None) in procs and MainProcess(511, None) in procs

def test_v1_does_not_match_v12():
    procs = main_processes(parse_ps(FIXTURE), BIN)
    assert next(p for p in procs if p.pid == 501).data_dir == AS / "Claude-V1"
    assert next(p for p in procs if p.pid == 502).data_dir == AS / "Claude-V12"

def test_match_instances_argless_goes_to_default_launcher_and_duplicates():
    launchers = [L("Claude V1", AS / "Claude-V1"), L("Claude Personal", AS / "Claude"), L("Claude Dup", AS / "Claude-Dup"), L("Claude Idle", AS / "Claude-Idle")]
    by_name, strays = match_instances(main_processes(parse_ps(FIXTURE), BIN), launchers, AS / "Claude")
    assert by_name["Claude Personal"] == [Running(504, argless=True), Running(511, argless=True)]
    assert [r.pid for r in by_name["Claude Dup"]] == [505, 506]
    assert by_name["Claude Idle"] == []
    assert [p.pid for p in strays] == [502]

def test_argless_is_stray_when_no_default_launcher(): ...   # no launcher at AS/"Claude" -> 504 and 511 in strays
def test_watchdog_pids_match_script_path_only():
    assert watchdog_pids(parse_ps(FIXTURE), Path("/Apps/Claude V1.app/Contents/MacOS/launcher")) == [508]
def test_watchdog_alive_requires_lock_and_live_cmdline(tmp_path):
    os.symlink("508", tmp_path / "Claude V1.lock")
    assert watchdog_alive("Claude V1", Path("/Apps/Claude V1.app/Contents/MacOS/launcher"), parse_ps(FIXTURE), tmp_path)
    os.remove(tmp_path / "Claude V1.lock"); os.symlink("999", tmp_path / "Claude V1.lock")
    assert not watchdog_alive("Claude V1", Path("/Apps/Claude V1.app/Contents/MacOS/launcher"), parse_ps(FIXTURE), tmp_path)
def test_claude_binary_reads_plist(tmp_path): ...            # CFBundleExecutable "stubclaude" -> ".../MacOS/stubclaude"; missing plist -> ".../MacOS/Claude"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_processes.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `processes.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_processes.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/processes.py tests/test_processes.py tests/fixtures/ps-output.txt
git commit -m "feat(tui): match Claude main processes to launchers, argless and duplicate rules"
```

---

### Task 8: Account identity, expected accounts, mismatch warnings

**Files:**
- Create: `src/claude_instances/accounts.py`, `tests/test_accounts.py`

**Interfaces:**
- Consumes: `SessionState` (Task 3).
- Produces: `last_known_account_uuid(data_dir: Path) -> str | None` (reads `<data_dir>/config.json` key `lastKnownAccountUuid`; None if missing or malformed); `short_id(uuid: str) -> str` (first 8 chars); `class ExpectedAccounts` with `load(path: Path) -> ExpectedAccounts`, `save(path: Path) -> None` (writes `{"<name>": "<uuid>"}` with mode 0600), `get(name) -> str | None`, `set(name, uuid)`; `@dataclass AccountRow(name: str, account: str | None, state: SessionState)`; `shared_account_groups(rows: list[AccountRow]) -> dict[str, list[str]]` (name -> other names on the same account, counting only SIGNED_IN and REJECTED rows); `expected_mismatch(row: AccountRow, expected: ExpectedAccounts) -> bool` (False when no expectation recorded or row.account is None).

- [ ] **Step 1: Write the failing tests**

```python
A = "11111111-2222-4333-8444-555555555555"; B = "22222222-2222-4333-8444-555555555555"

def test_last_known_account_uuid(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"lastKnownAccountUuid": A}))
    assert last_known_account_uuid(tmp_path) == A and short_id(A) == "11111111"
def test_missing_or_malformed_config_is_none(tmp_path): ...   # no file -> None; b"{" -> None
def test_shared_only_counts_signed_in():
    rows = [AccountRow("P", A, SIGNED_IN), AccountRow("C", A, SIGNED_IN), AccountRow("X", A, SIGNED_OUT), AccountRow("V", B, SIGNED_IN)]
    assert shared_account_groups(rows) == {"P": ["C"], "C": ["P"]}
def test_expected_mismatch():
    e = ExpectedAccounts(); e.set("P", A)
    assert expected_mismatch(AccountRow("P", B, SIGNED_IN), e)
    assert not expected_mismatch(AccountRow("P", A, SIGNED_IN), e)
    assert not expected_mismatch(AccountRow("C", B, SIGNED_IN), e)
def test_expected_accounts_roundtrip_and_mode(tmp_path):
    e = ExpectedAccounts(); e.set("P", A); e.save(tmp_path / "x.json")
    assert ExpectedAccounts.load(tmp_path / "x.json").get("P") == A
    assert oct(os.stat(tmp_path / "x.json").st_mode & 0o777) == "0o600"
def test_load_missing_file_is_empty(tmp_path): ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_accounts.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `accounts.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_accounts.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/accounts.py tests/test_accounts.py
git commit -m "feat(tui): account identity, expected accounts and mismatch checks"
```

---

### Task 9: Usage model, cached sources, TUI cache, lookup types and display formatting

**Files:**
- Create: `src/claude_instances/usage.py`, `tests/test_usage.py`, `tests/fixtures/plan-usage-history.json`

**Interfaces:**
- Produces: constants `WARN_PCT = 80`, `CRIT_PCT = 95`, `LIVE_INTERVAL_S = 300`, `LIVE_STALE_AFTER_S = 360`, `NO_DATA = "\N{EM DASH}"`; `class Source(Enum): LIVE, TUI_CACHE, CLAUDE_HISTORY`; `@dataclass(frozen=True) Usage(five_hour_pct: float | None, five_hour_resets_at: datetime | None, week_pct: float | None, week_resets_at: datetime | None, fetched_at: datetime, source: Source)`; `parse_plan_usage_history(text: str, org: str | None) -> Usage | None` (version must be 2; with `org`, the latest sample by `t` for that org; with `org is None`, the latest sample only when every sample has the same `org`, else None, so an account switch never shows another org's numbers; `fh` -> five_hour_pct, `sd` -> week_pct, reset times None, `fetched_at` from `t` ms UTC, source CLAUDE_HISTORY; None on any malformation); `read_claude_history(data_dir: Path, org: str | None) -> Usage | None`; `account_hash(uuid: str) -> str` (first 12 hex of sha256); `class UsageCache` with `load(path) -> UsageCache` (corrupt or missing -> empty), `save(path)` (mode 0600, only the fields `five_hour_pct, five_hour_resets_at, week_pct, week_resets_at, fetched_at, account_hash` per display name, ISO timestamps), `get(name: str, account_uuid: str | None) -> Usage | None` (None when `account_uuid` is None or the stored `account_hash` differs; source TUI_CACHE), `put(name, account_uuid: str | None, usage)` (no-op when `account_uuid` is None), `stored_hash(name) -> str | None` (the `account_hash` saved with that name's entry); `best_cached(tui: Usage | None, claude: Usage | None) -> Usage | None` (the newer `fetched_at`, TUI wins ties); `level(pct: float | None) -> Literal["ok", "warn", "crit", "none"]`; `format_age(delta: timedelta) -> str` (`"<1m"`, `"5m"`, `"2h"`, `"37d"`); `format_until(delta: timedelta) -> str` (`"2h10m"` below one day, `"3d"` at or above; negative -> `"now"`); `format_cell(pct: float | None, resets_at: datetime | None, usage: Usage | None, now: datetime) -> str` (`NO_DATA` when pct is None; LIVE: `"12%  2h10m"`, or `"12%"` with no reset time; cached: `"44% · 37d old"`).
- Lookup contract (so Tasks 12 to 16 do not wait for Checkpoint A): `@dataclass LookupResult(usage: Usage | None, email: str | None = field(repr=False), org: str | None = field(repr=False), rejected: bool, error: str | None)` (account details `repr=False` so a traceback never prints them; field order may be adjusted so defaults come last); `class Lookup(Protocol): def lookup(self, data_dir: Path) -> LookupResult` (must never raise).

- [ ] **Step 1: Write the fixture and failing tests**

`tests/fixtures/plan-usage-history.json`: version 2 with three samples over two made-up orgs (`aaaaaaaa-...` and `bbbbbbbb-...`), the latest for org A at `t = 1793966400000` (2026-11-06T12:00:00Z) with `fh: 44, sd: 12`.

```python
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)

def test_history_latest_for_org():
    u = parse_plan_usage_history(FIXTURE, ORG_A)
    assert (u.five_hour_pct, u.week_pct, u.fetched_at, u.source) == (44, 12, NOW, Source.CLAUDE_HISTORY)
def test_history_no_org_requires_single_org():
    assert parse_plan_usage_history(FIXTURE, None) is None              # two orgs present
    single = json.dumps({"version": 2, "samples": [{"t": 1793966400000, "org": ORG_A, "u": {"fh": 5, "sd": 6}}]})
    assert parse_plan_usage_history(single, None).five_hour_pct == 5
def test_history_malformed_is_none():
    for text in ["", "{", '{"version": 1, "samples": []}', '{"version": 2, "samples": []}', '{"version": 2, "samples": [{"t": "x"}]}']:
        assert parse_plan_usage_history(text, ORG_A) is None
def test_cache_roundtrip_and_fields_only(tmp_path):
    c = UsageCache(); c.put("Claude P", ORG_A, Usage(81, NOW + timedelta(minutes=40), 52, NOW + timedelta(days=5), NOW, Source.LIVE))
    c.save(tmp_path / "u.json")
    raw = json.loads((tmp_path / "u.json").read_text())
    assert set(raw["Claude P"]) == {"five_hour_pct", "five_hour_resets_at", "week_pct", "week_resets_at", "fetched_at", "account_hash"}
    assert ORG_A not in (tmp_path / "u.json").read_text()
    got = UsageCache.load(tmp_path / "u.json").get("Claude P", ORG_A)
    assert got.five_hour_pct == 81 and got.source is Source.TUI_CACHE
def test_cache_discarded_on_account_change_and_none(tmp_path):
    c = UsageCache(); c.put("P", ORG_A, LIVE_USAGE)
    assert c.get("P", ORG_B) is None and c.get("P", None) is None
    c.put("Q", None, LIVE_USAGE); assert c.get("Q", None) is None
def test_cache_corrupt_file_is_empty(tmp_path): ...         # b"{{{" -> load ok, get -> None
def test_best_cached_prefers_newer(): ...
def test_level_thresholds():
    assert [level(x) for x in (None, 0, 79.9, 80, 94.9, 95, 120)] == ["none", "ok", "ok", "warn", "warn", "crit", "crit"]
def test_format_until_and_age():
    assert format_until(timedelta(hours=2, minutes=10)) == "2h10m" and format_until(timedelta(days=3, hours=5)) == "3d"
    assert format_until(timedelta(minutes=40)) == "0h40m" and format_until(timedelta(seconds=-5)) == "now"
    assert format_age(timedelta(days=37, hours=3)) == "37d" and format_age(timedelta(seconds=30)) == "<1m"
def test_format_cell():
    live = Usage(12, NOW + timedelta(hours=2, minutes=10), 44, NOW + timedelta(days=3), NOW, Source.LIVE)
    cached = Usage(44, None, None, None, NOW - timedelta(days=37), Source.TUI_CACHE)
    assert format_cell(live.five_hour_pct, live.five_hour_resets_at, live, NOW) == "12%  2h10m"
    assert format_cell(cached.five_hour_pct, None, cached, NOW) == "44% · 37d old"
    assert format_cell(None, None, cached, NOW) == NO_DATA
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_usage.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `usage.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_usage.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/usage.py tests/test_usage.py tests/fixtures/plan-usage-history.json
git commit -m "feat(tui): usage model, cached sources, TUI usage cache and formatting"
```

---

### Task 10: Live usage lookup (after CHECKPOINT A)

**Files:**
- Create: `src/claude_instances/api.py`, `tests/test_api.py`, `tests/fixtures/usage-response.json`, `tests/fixtures/account-response.json`

**Interfaces:**
- Consumes: `HttpClient`, `HttpResult`, path constants, `FakeHttp` (Task 5); `Secret` (Task 2); `read_claude_cookies` (Task 3); `derive_key`, `decrypt_store`, `make_signed_in_dir` (Task 4); `Usage`, `Source`, `LookupResult`, `Lookup` (Task 9).
- Produces: `parse_usage_response(data: dict, fetched_at: datetime) -> Usage | None` (field names fixed from the probe output; source LIVE; None when expected keys are absent); `parse_account_email(data: dict) -> str | None` (key fixed from the probe, or always None if the probe showed no email-like field; then the README says live mode shows short ids); `parse_org_uuid(data: Any) -> str | None` (first org from the orgs response); `class UsageLookup(Lookup)` with `__init__(self, http: HttpClient, key: Secret, now=lambda: datetime.now(timezone.utc))` and `lookup(self, data_dir: Path) -> LookupResult`. Outcome rules: no cookie store or no `sessionKey` -> `error="no session cookie"`; decryption failure -> `error="cannot decrypt cookies (wrong key?)"`; usage call 401, or 403 with `kind == "json"` -> `rejected=True, error=None`; 403 with `kind == "html"` -> `error="live unavailable (403)"`; other non-200 -> `error=f"live unavailable ({status})"`; `kind == "error"` -> `error=f"live unavailable ({error})"`; any exception -> `error=f"lookup failed ({type(e).__name__})"`. The method never raises and `error` never contains URLs, ids, headers, cookie values or body text.

- [ ] **Step 1: Write the fixtures and failing tests**

Fixtures mirror the probe's confirmed shapes with made-up values (percentages, ISO reset times, uuid `11111111-...`, `user@example.com`). Every lookup test starts from `make_signed_in_dir(tmp_path, PW)` (or `org=None`) and `KEY = PW`.

```python
def test_parse_usage_response_from_fixture():
    u = parse_usage_response(USAGE_FIXTURE, NOW)
    assert u.source is Source.LIVE and 0 <= u.five_hour_pct <= 100 and u.five_hour_resets_at.tzinfo is not None
def test_parse_usage_missing_keys_is_none(): assert parse_usage_response({}, NOW) is None
def test_lookup_happy_path(tmp_path):
    make_signed_in_dir(tmp_path, PW); http = FakeHttp(usage=HttpResult(200, USAGE_FIXTURE, "json"), account=HttpResult(200, ACCOUNT_FIXTURE, "json"))
    r = UsageLookup(http, PW, now=lambda: NOW).lookup(tmp_path)
    assert r.usage.five_hour_pct == USAGE_FIXTURE_PCT and (r.rejected, r.error, r.org) == (False, None, ORG_A)
    assert r.email == parse_account_email(ACCOUNT_FIXTURE)
    assert f"/api/organizations/{ORG_A}/usage" in http.paths
def test_lookup_without_lastactiveorg_lists_orgs(tmp_path):
    make_signed_in_dir(tmp_path, PW, org=None); http = FakeHttp(orgs=HttpResult(200, [{"uuid": ORG_A}], "json"), usage=...)
    UsageLookup(http, PW).lookup(tmp_path); assert http.paths[0] == "/api/organizations"
def test_html_403_is_unavailable_not_rejected(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    http = FakeHttp(usage=HttpResult(403, None, "html"))
    r = UsageLookup(http, PW).lookup(tmp_path)
    assert f"/api/organizations/{ORG_A}/usage" in http.paths
    assert (r.rejected, r.usage, r.error) == (False, None, "live unavailable (403)")
def test_json_401_is_rejected(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    r = UsageLookup(FakeHttp(usage=HttpResult(401, {"error": "x"}, "json")), PW).lookup(tmp_path)
    assert (r.rejected, r.error) == (True, None)
def test_wrong_key_is_decrypt_error(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    assert UsageLookup(FakeHttp(), Secret("other")).lookup(tmp_path).error == "cannot decrypt cookies (wrong key?)"
def test_exception_inside_lookup_is_caught(tmp_path):
    make_signed_in_dir(tmp_path, PW)
    http = RaisingHttp(RuntimeError("sk-ant-secret"))
    r = UsageLookup(http, PW).lookup(tmp_path)
    assert http.called and r.error == "lookup failed (RuntimeError)"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `api.py`**

If the probe (Checkpoint A) showed a different path or shape, update the constants in `httpclient.py` and the parser here; do not guess.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/api.py tests/test_api.py tests/fixtures/usage-response.json tests/fixtures/account-response.json
git commit -m "feat(tui): live usage lookup against the confirmed claude.ai shape"
```

---

### Task 11: Launch order file

**Files:**
- Create: `src/claude_instances/order.py`, `tests/test_order.py`

**Interfaces:**
- Produces: `load_order(path: Path) -> list[str]` (one name per line, blank lines dropped, a final line without newline kept, missing file -> `[]`); `save_order(path: Path, names: list[str]) -> None` (de-duplicated, creates the parent dir); `apply_order(names: list[str], order: list[str]) -> list[str]` (names in `order` first, in that order, skipping unknown and repeated entries; the rest appended with plain `sorted()`); `move_name(ordered: list[str], name: str, delta: int) -> list[str]` (new full list with `name` moved by `delta` positions, clamped).

- [ ] **Step 1: Write the failing tests**

```python
def test_apply_order_ignores_unknown_and_duplicates():
    assert apply_order(["B", "A", "C"], ["C", "Zed", "C", "A"]) == ["C", "A", "B"]
def test_apply_order_empty_is_codepoint_sorted(): assert apply_order(["b", "B", "A"], []) == ["A", "B", "b"]
def test_move_name_clamps():
    assert move_name(["A", "B", "C"], "C", -1) == ["A", "C", "B"]
    assert move_name(["A", "B", "C"], "A", -1) == ["A", "B", "C"]
def test_roundtrip_dedup(tmp_path):
    save_order(tmp_path / "cfg/order.txt", ["B", "A", "B"])
    assert (tmp_path / "cfg/order.txt").read_text() == "B\nA\n"
    assert load_order(tmp_path / "cfg/order.txt") == ["B", "A"]
def test_load_without_trailing_newline(tmp_path):
    (tmp_path / "o.txt").write_text("B\nA"); assert load_order(tmp_path / "o.txt") == ["B", "A"]
def test_load_missing_is_empty(tmp_path): assert load_order(tmp_path / "none") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_order.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `order.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_order.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/order.py tests/test_order.py
git commit -m "feat(tui): launch order file"
```

---

### Task 12: Snapshot assembly

**Files:**
- Create: `src/claude_instances/snapshot.py`, `tests/test_snapshot.py`
- Modify: `tests/helpers.py` (add `make_data_dir`)

**Interfaces:**
- Consumes: Tasks 3, 6, 7, 8, 9, 11.
- Produces:
  - `@dataclass(frozen=True) LiveEntry(account: str | None, usage: Usage)`.
  - `@dataclass(frozen=True) RejectedMark(account: str | None, session_created: int | None)`.
  - `@dataclass(frozen=True) AccountInfo(email: str | None = field(default=None, repr=False), org: str | None = field(default=None, repr=False))`, keyed by account UUID, never by display name, so an account switch never shows the previous account's email or org.
  - `@dataclass InstanceState(name: str, launcher: Launcher, position: int, running: list[Running], session: SessionState, session_expiry: datetime | None, session_created: int | None, account: str | None = field(repr=False), email: str | None = field(repr=False), org: str | None = field(repr=False), expected: str | None = field(repr=False), shared_with: list[str], expected_mismatch: bool, usage: Usage | None, live_error: str | None, watchdog_alive: bool)` (account details are `repr=False` so they never reach a traceback), with properties `pids -> list[int]`, `is_running -> bool` (exactly one), `is_duplicate -> bool` (two or more), `argless -> bool`, `pid -> int | None` (only when exactly one).
  - `@dataclass Snapshot(rows: list[InstanceState], strays: list[MainProcess], taken_at: datetime)` with `row(name) -> InstanceState`.
  - `@dataclass Inputs(launchers: list[Launcher], ps_lines: list[ProcessLine], default_data_dir: Path, order: list[str], expected: ExpectedAccounts, usage_cache: UsageCache, log_dir: Path, now: datetime, live: dict[str, LiveEntry], account_info: dict[str, AccountInfo], live_errors: dict[str, str], rejected: dict[str, RejectedMark])`.
  - `build_snapshot(inp: Inputs) -> Snapshot`.
- Rules:
  - rows are in launch order;
  - `email` and `org` come from `account_info.get(account)`;
  - `session` is REJECTED only while `rejected[name]` equals the row's current `(account, session_created)`, so a new sign-in or account change clears it;
  - `usage` is `live[name].usage` when `live[name].account == account` and `now - fetched_at <= LIVE_STALE_AFTER_S`; a matching but older live entry is shown as `replace(usage, source=Source.TUI_CACHE)`;
  - otherwise `usage` is `best_cached(usage_cache.get(name, account), history)`, where `history = read_claude_history(data_dir, org)`. When `org` is None, org-less history is used only if `usage_cache.stored_hash(name)` is None or equals `account_hash(account)`; a cache entry from a different account means the history may belong to it too;
  - `shared_with` and `expected_mismatch` come from Task 8.

- [ ] **Step 1: Write the failing tests**

`make_data_dir(root: Path, account: str | None, session_expiry: datetime | None | Literal["none"], history: str | None, created: int = 1) -> Path` builds `config.json`, `Cookies` and optionally `plan-usage-history.json`.

```python
def test_snapshot_rows_in_order_with_states(tmp_path):
    # launchers P (default dir, argless proc 504), C (running 501, same account as P), I (closed, signed out, single-org history 44%)
    snap = build_snapshot(inputs)
    assert [r.name for r in snap.rows] == ["Claude C", "Claude P", "Claude I"]   # order.txt lists C first
    p = row("Claude P"); assert p.is_running and p.argless and p.pid == 504
    assert row("Claude C").shared_with == ["Claude P"] and row("Claude P").shared_with == ["Claude C"]
    i = row("Claude I"); assert not i.is_running and i.session is SessionState.SIGNED_OUT
    assert i.usage.five_hour_pct == 44 and i.usage.source is Source.CLAUDE_HISTORY
def test_live_usage_shown_live_then_downgraded(tmp_path):
    inp.live = {"Claude C": LiveEntry(A, Usage(12, ..., fetched_at=NOW, source=Source.LIVE))}
    assert row_of(build_snapshot(inp), "Claude C").usage.source is Source.LIVE
    inp.now = NOW + timedelta(seconds=LIVE_STALE_AFTER_S + 1)
    assert row_of(build_snapshot(inp), "Claude C").usage.source is Source.TUI_CACHE
def test_live_entry_for_other_account_ignored(tmp_path): ...   # LiveEntry(B, ...) on a row now on A -> not used
def test_history_uses_org_of_current_account(tmp_path): ...   # two-org history, account_info={A: AccountInfo(org=ORG_B)} -> ORG_B's sample
def test_orgless_history_skipped_when_cache_knows_other_account(tmp_path):
    # single-org history; usage_cache holds an entry for "Claude I" fetched for account B; row is now on A
    assert row_of(build_snapshot(inp), "Claude I").usage is None
def test_email_keyed_by_account(tmp_path):
    inp.account_info = {B: AccountInfo(email="b@example.com")}       # row is on A
    assert row_of(build_snapshot(inp), "Claude C").email is None
    assert "example.com" not in repr(row_of(build_snapshot(replace(inp, account_info={A: AccountInfo(email="a@example.com")})), "Claude C"))
def test_duplicate_flag(tmp_path): ...                         # two Running -> is_duplicate, pid None, pids == [505, 506]
def test_tui_cache_beats_older_history(tmp_path): ...
def test_rejected_cleared_by_new_session(tmp_path):
    inp.rejected = {"Claude C": RejectedMark(A, 1)}
    assert row_of(build_snapshot(inp), "Claude C").session is SessionState.REJECTED
    remake Cookies with created=2
    assert row_of(build_snapshot(inp), "Claude C").session is SessionState.SIGNED_IN
def test_watchdog_alive_in_row(tmp_path): ...                  # lock symlink + ps line with script path -> True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_snapshot.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `snapshot.py` and `make_data_dir`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_snapshot.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/snapshot.py tests/helpers.py tests/test_snapshot.py
git commit -m "feat(tui): assemble per-instance snapshot from local reads"
```

---

### Task 13: System actions, guarded launch, quit flow, launch all

**Files:**
- Create: `src/claude_instances/actions.py`, `tests/test_actions.py`
- Modify: `tests/helpers.py` (add `FakeSystemActions`)

**Interfaces:**
- Consumes: `InstanceState`, `Snapshot` (Task 12); `ProcessLine`, `watchdog_pids`, `main_processes`, `match_instances`, `claude_binary` (Task 7); `Launcher` (Task 6).
- Produces: `@dataclass ActionResult(ok: bool, message: str)`; `class SystemActions(Protocol)`: `bring_to_front(pid: int) -> ActionResult`, `launch(app_path: Path) -> ActionResult`, `send_quit(pid: int) -> ActionResult`, `signal_pids(pids: list[int], sig: int) -> None`, `is_alive(pid: int) -> bool`, `processes() -> list[ProcessLine]`; `class MacSystemActions(SystemActions)` with `launch_argv(app_path) -> list[str]` = `["open", "-n", str(app_path)]` and `front_argv(pid) -> list[str]` = `["osascript", "-e", f'tell application "System Events" to set frontmost of (first process whose unix id is {pid}) to true']`; `send_quit` is `os.kill(pid, QUIT_SIGNAL)` with `QUIT_SIGNAL = signal.SIGTERM` (confirmed at Checkpoint A; if that check was not clean, `send_quit` instead calls `NSRunningApplication.runningApplicationWithProcessIdentifier_(pid).terminate()` via the added dependency `pyobjc-framework-Cocoa`; never anything that addresses Claude by app name); `automation_denied(stderr: str) -> bool` (true on `-1743`, `not allowed assistive access` or `Not authorized`); `AUTOMATION_HELP = "Allow your terminal to control System Events: System Settings > Privacy & Security > Automation > <terminal> > System Events"`.
- Guarded launch: `launch_refusal(launcher: Launcher, procs: list[MainProcess], default_data_dir: Path, rearm: bool = False) -> str | None` returns: `"old one-shot launcher; rebuild it with new-claude before launching from here"` when not `launcher.is_watchdog`; `"already running"` when not `rearm` and any proc matches the launcher (by `match_instances`); `"launcher predates the argless rule and an argless Claude is running on its folder; rebuild it first"` when `launcher.data_dir == default_data_dir`, `not launcher.has_argless_rule` and an argless proc exists; else None. `guarded_launch(launcher, actions, default_data_dir, rearm=False) -> ActionResult` reads `actions.processes()` fresh, computes `main_processes(..., claude_binary(launcher.claude_app))`, refuses with the reason or calls `actions.launch(launcher.app_path)`.
- Quit: `stop_watchdogs(script_paths: list[Path], actions, sleep=time.sleep, timeout_s: int = WATCHDOG_STOP_TIMEOUT_S) -> list[int]` (SIGTERM every pid whose command contains a script path; poll `processes()` every 0.5 s up to `timeout_s`; SIGKILL survivors and poll 2 s more; return pids still alive; never touches a Claude pid because Claude's command line never contains the launcher path); `quit_pids(pids: list[int], actions, sleep, timeout_s: int = QUIT_TIMEOUT_S) -> list[int]` (send_quit each, poll `is_alive` every 1 s, return survivors); `class QuitOutcome(Enum): QUIT, DID_NOT_QUIT, WATCHDOG_STUCK, NOT_RUNNING`; `quit_instance(state: InstanceState, actions, sleep=time.sleep) -> tuple[QuitOutcome, str]` (stop watchdog; if any survive -> `(WATCHDOG_STUCK, "watchdog did not stop; instance left running")` without quitting; else quit all of `state.pids`; survivors -> `(DID_NOT_QUIT, "did not quit; watchdog stopped")`); `rearm_watchdog(state, actions, default_data_dir) -> ActionResult` (= `guarded_launch(..., rearm=True)`); `launch_all(snapshot: Snapshot, actions, default_data_dir, sleep=time.sleep, gap_s: int = LAUNCH_GAP_S, launch_one: Callable[[Launcher], ActionResult] | None = None) -> list[tuple[str, ActionResult]]` (walks rows in order, skips rows the snapshot shows running or duplicate, calls `launch_one` (default: `guarded_launch`) for the rest, `sleep(gap_s)` between consecutive successful launches; Task 16 passes its pending-launch-aware launcher).
- `tests/helpers.py`: `FakeSystemActions(ps: list[ProcessLine] = [], alive: set[int] = set(), quit_after: int = 1, never_dies: set[int] = set(), front_result: ActionResult | None = None, on_launch: Callable[[Path], None] | None = None, before_call: Callable[[str], None] | None = None)`. Every method first calls `before_call(method_name)` when set, then appends `(method_name, *args)` to `calls` (`processes()` records `("processes",)`); `launched: list[Path]` collects launch targets; `send_quit`/`signal_pids` remove the pids from `alive` and `ps` after `quit_after` later `processes()`/`is_alive()` polls unless the pid is in `never_dies` (SIGKILL removes immediately unless in `never_dies`); `launch` calls `on_launch(app_path)` when set (used to simulate the instance appearing). Reused by Tasks 14, 16 and 17.

- [ ] **Step 1: Write the failing tests**

```python
WD = "/Apps/Claude C.app/Contents/MacOS/launcher"

def test_quit_stops_and_waits_for_watchdog_then_quits():
    fake = FakeSystemActions(alive={501, 508}, ps=[ProcessLine(508, f"/bin/zsh {WD}"), ProcessLine(501, "claude ...")])
    outcome, _ = quit_instance(state_running([501], script=WD), fake, sleep=lambda s: None)
    signals = [c for c in fake.calls if c[0] in ("signal_pids", "send_quit")]
    assert signals[:2] == [("signal_pids", [508], signal.SIGTERM), ("send_quit", 501)]
    w, q = fake.calls.index(signals[0]), fake.calls.index(signals[1])
    assert ("processes",) in fake.calls[w + 1:q]      # watchdog exit confirmed by a ps poll before the quit
    assert outcome is QuitOutcome.QUIT
def test_stuck_watchdog_escalates_then_aborts():
    fake = FakeSystemActions(alive={501, 508}, ps=[...], never_dies={508})
    assert quit_instance(state_running([501], script=WD), fake, sleep=lambda s: None)[0] is QuitOutcome.WATCHDOG_STUCK
    assert ("signal_pids", [508], signal.SIGKILL) in fake.calls and ("send_quit", 501) not in fake.calls
def test_quit_timeout_reports_watchdog_stopped():
    fake = FakeSystemActions(alive={501}, ps=[ProcessLine(501, "claude")], never_dies={501})
    assert quit_instance(state_running([501]), fake, sleep=lambda s: None) == (QuitOutcome.DID_NOT_QUIT, "did not quit; watchdog stopped")
def test_quit_duplicate_quits_both_pids(): ...      # state_running([505, 506]) -> send_quit for both
def test_guarded_launch_refuses_old_oneshot(): ...  # fmt="oneshot" -> ok False, nothing launched
def test_guarded_launch_refuses_pre_argless_launcher_on_default_with_argless_running():
    fake = FakeSystemActions(ps=[ProcessLine(504, BIN)], alive={504})
    r = guarded_launch(launcher(fmt="watchdog", data_dir=DEFAULT), fake, DEFAULT, rearm=True)
    assert not r.ok and "argless" in r.message and fake.launched == []
def test_guarded_launch_rereads_ps_and_refuses_running(): ...   # snapshot said closed, fresh ps shows it -> "already running"
def test_launch_all_order_skip_and_gap():
    slept = []
    results = launch_all(snapshot(rows=[closed("B"), running("A"), closed("C"), duplicate("D")]), fake, DEFAULT, sleep=slept.append)
    assert [n for n, r in results if r.ok] == ["B", "C"] and slept == [3] and fake.launched == [Path("/Apps/B.app"), Path("/Apps/C.app")]
def test_launch_and_front_argv():
    assert MacSystemActions().launch_argv(Path("/Apps/X.app")) == ["open", "-n", "/Apps/X.app"]
    argv = " ".join(MacSystemActions().front_argv(501)); assert "unix id is 501" in argv and 'application "Claude"' not in argv
def test_automation_denied_detected():
    assert automation_denied("execution error: Not authorized to send Apple events to System Events. (-1743)")
    assert not automation_denied("")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_actions.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `actions.py` and `FakeSystemActions`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_actions.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/actions.py tests/helpers.py tests/test_actions.py
git commit -m "feat(tui): system actions, guarded launch, watchdog-first quit and launch-all"
```

---

### Task 14: Sign-in helper flow

**Files:**
- Create: `src/claude_instances/signin.py`, `tests/test_signin.py`
- Modify: `tests/helpers.py` (add `ScriptedRefresh`)

**Interfaces:**
- Consumes: `SystemActions`, `stop_watchdogs`, `quit_pids`, `guarded_launch`, `FakeSystemActions` (Task 13); `Snapshot`, `InstanceState` (Task 12); `RESTORE_FILE` (Task 1).
- Produces: `class SigninPhase(Enum): QUITTING, LAUNCHING, ISOLATING, WAITING_FOR_LOGIN, RESTORING, DONE, FAILED`; `class SigninRefused(Exception)`; `@dataclass SigninPlan(target: str, quit_rows: list[str], quit_pids: list[int], watchdog_rows: list[str], restore: list[str], not_restorable: list[str])`.
- `plan_signin(snapshot: Snapshot, target: str) -> SigninPlan`:
  - raises `SigninRefused("target has more than one process on its folder; quit one first")` when the target row `is_duplicate`, and `SigninRefused("old one-shot launcher; rebuild it with new-claude first")` when the target is not running and its launcher is not `is_watchdog`;
  - `quit_rows` = every other row with any pid (running or duplicate);
  - `watchdog_rows` = every other row whose `watchdog_alive` is true, running or not (a watchdog in its grace or update wait can reopen its instance mid-login);
  - `quit_pids` = all pids of `quit_rows` plus every stray main process pid (argless strays and unknown data dirs alike; when the target is the default-folder launcher, its argless pid is the target's own and is not quit);
  - `restore` = rows in `quit_rows` or `watchdog_rows` that are not duplicates and whose launcher `is_watchdog`, in launch order;
  - `not_restorable` = `"<name> (duplicate; pids a, b)"` per duplicate row, `"<name> (old one-shot launcher)"` per quit one-shot row, and `"argless Claude (pid N)"` or `"<data dir basename> (pid N)"` per stray. They are quit and not relaunched; the confirmation lists them.
- Restore file: first line `owner <pid>`, then one name per line. `write_restore(path, names, owner_pid: int = os.getpid())`, `read_restore(path) -> tuple[int | None, list[str]]` (missing file -> `(None, [])`), `clear_restore(path)`.
- `@dataclass SigninResult(ok: bool, message: str, new_account: str | None = field(repr=False), restored: list[str], restore_failed: list[tuple[str, str]], warnings: list[str])`.
- `class SigninFlow` with `__init__(self, actions: SystemActions, refresh: Callable[[], Snapshot], config_dir: Path, default_data_dir: Path, sleep=time.sleep, on_phase: Callable[[SigninPhase, str], None] = lambda p, m: None)` and `run(self, plan: SigninPlan, cancelled: threading.Event) -> SigninResult`, implementing spec steps 2 to 8:
  1. write the restore file with `plan.restore` before anything else;
  2. QUITTING: `stop_watchdogs` for the launchers of `quit_rows` and `watchdog_rows` (survivors -> FAILED `"watchdog did not stop: <names>"`), then `quit_pids(plan.quit_pids)` (survivors -> FAILED `"did not quit: <names or pids>"`);
  3. LAUNCHING: if the target is not running, `guarded_launch` it; an "already running" refusal counts as success (the fresh `ps` saw it); any other refusal is FAILED immediately with the refusal reason (no polling); otherwise poll `refresh()` every 2 s for up to `LAUNCH_TIMEOUT_S` until the target row `is_running`; else FAILED `"<name> did not start"`;
  4. ISOLATING: the target must be the only Claude running. While the latest snapshot shows any main pid that is not the target's (for example an argless Claude ShipIt relaunched while nothing was running), stop the watchdog of its row if it has one, `quit_pids` it, add its row (when restorable) to the restore list and rewrite the restore file, append `"quit <row or stray description> (pid N) that started during sign-in"` to `warnings`, and refresh again; survivors -> FAILED. WAITING_FOR_LOGIN is never reported before a snapshot shows the target running alone;
  5. record `account0` and `created0` from that snapshot (`created0` is None for a signed-out target with no `sessionKey` row);
  6. WAITING_FOR_LOGIN: poll `refresh()` every 2 s until `cancelled`, or the target's `account != account0`, or the cookie is fresh: `created is not None and (created0 is None or created > created0)` (covers the ~30 s cookie flush); any new non-target main pid is appended once to `warnings` as `"another Claude started (pid N); the login may land there"` and reported via `on_phase`;
  7. `finally` (always, including cancel and exceptions): RESTORING: for each name in the restore list, in launch order with `LAUNCH_GAP_S` between launches: `guarded_launch(..., rearm=True)` when a fresh `ps` shows it still running (it refused to quit; this re-arms its watchdog), else `guarded_launch`. Successes go to `restored`, refusals to `restore_failed` as `(name, reason)`. Then rewrite the restore file with only the failed names, or `clear_restore` when none failed.
  Exceptions inside `run` are caught and turned into `ok=False` with `message=f"sign-in failed ({type(e).__name__})"`; cancel gives `ok=False, message="cancelled"`.
- `tests/helpers.py`: `ScriptedRefresh(items: list[Snapshot | Exception], on_call: dict[int, Callable[[], None]] = {})`, a callable whose n-th call (1-based, counted in `calls`) first runs `on_call.get(n)`, then raises the item if it is an exception or returns it (the last item repeats once the list is exhausted); returned snapshots are recorded in `returned`.

- [ ] **Step 1: Write the failing tests**

Fixture `snap0`, rows in launch order: `P` (default folder, argless pid 504, watchdog alive), `C` (pid 501, watchdog alive), `T` (target, closed, signed out with no `sessionKey` row, account A), `D` (duplicate 505, 506), `W` (not running, watchdog alive: in its update wait), `O` (old one-shot launcher, pid 507); one stray on `Claude-Unknown` (pid 502). `fake` is `FakeSystemActions` with all those pids alive and `quit_after=1`; `APP[name]` is each launcher's `.app` path; `flow = SigninFlow(fake, refresh, tmp_path, DEFAULT, sleep=noop)`.

```python
def test_plan_quits_everything_but_target():
    p = plan_signin(snap0, "T")
    assert set(p.quit_rows) == {"P", "C", "D", "O"} and set(p.quit_pids) == {504, 501, 505, 506, 507, 502}
    assert set(p.watchdog_rows) == {"P", "C", "W"} and p.restore == ["P", "C", "W"]
    assert p.not_restorable == ["D (duplicate; pids 505, 506)", "O (old one-shot launcher)", "Claude-Unknown (pid 502)"]
def test_plan_target_default_keeps_its_argless_pid():
    assert 504 not in plan_signin(snap0, "P").quit_pids
def test_plan_refuses_duplicate_target():
    with pytest.raises(SigninRefused): plan_signin(snap0, "D")
def test_restore_file_written_before_any_quit(tmp_path):
    seen = []
    fake.before_call = lambda name: seen.append((name, read_restore(tmp_path / "signin-restore.txt")[1]))
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    first_quit = next(s for s in seen if s[0] in ("signal_pids", "send_quit"))
    assert first_quit[1] == ["P", "C", "W"] and r.ok
def test_signed_out_target_without_cookie_row(tmp_path):
    # refresh: T running alone, created None -> created None -> created 5
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert r.ok and r.message != "sign-in failed (TypeError)"
def test_success_restores_in_order_and_clears(tmp_path):
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert r.ok and r.restored == ["P", "C", "W"] and r.restore_failed == []
    assert fake.launched == [APP["T"], APP["P"], APP["C"], APP["W"]] and not (tmp_path / "signin-restore.txt").exists()
def test_success_on_account_change(tmp_path):
    # refresh: T alone account A -> account B
    assert flow.run(plan_signin(snap0, "T"), threading.Event()).new_account == B
def test_cancel_restores(tmp_path):
    ev = threading.Event(); refresh.on_call = {3: ev.set}           # call 3 is the first WAITING poll
    r = flow.run(plan_signin(snap0, "T"), ev)
    assert (r.ok, r.message, r.restored) == (False, "cancelled", ["P", "C", "W"]) and not (tmp_path / "signin-restore.txt").exists()
def test_exception_during_wait_still_restores(tmp_path):
    # refresh items: [T alone, RuntimeError("x")]
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert r.message == "sign-in failed (RuntimeError)" and r.restored == ["P", "C", "W"]
    assert not (tmp_path / "signin-restore.txt").exists()
def test_quit_timeout_fails_rearms_survivor_and_restores(tmp_path):
    fake.never_dies = {501}
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert not r.ok and "did not quit" in r.message and APP["T"] not in fake.launched
    assert r.restored == ["P", "C", "W"] and APP["C"] in fake.launched     # C re-armed, not left without a watchdog
def test_restore_refusal_kept_in_restore_file(tmp_path):
    # P's launcher predates the argless rule; an argless pid 600 is in ps when restoring
    r = flow.run(plan_signin(snap_with_old_default_launcher, "T"), threading.Event())
    assert [n for n, _ in r.restore_failed] == ["P"] and read_restore(tmp_path / "signin-restore.txt")[1] == ["P"]
def test_target_launch_refused_fails_immediately(tmp_path, monkeypatch):
    real = signin.guarded_launch
    monkeypatch.setattr(signin, "guarded_launch", lambda l, *a, **k: ActionResult(False, "refused for test") if l.name == "T" else real(l, *a, **k))
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert not r.ok and "refused for test" in r.message and refresh.calls == 0 and r.restored == ["P", "C", "W"]
def test_argless_appearing_while_launching_is_quit_before_waiting(tmp_path):
    # refresh items: [T running + stray argless 600, T alone, T alone created 5]
    phases = []; flow.on_phase = lambda p, m: phases.append((p, refresh.calls))
    r = flow.run(plan_signin(snap0, "T"), threading.Event())
    assert ("send_quit", 600) in fake.calls and any("pid 600" in w for w in r.warnings)
    first_wait = next(c for p, c in phases if p is SigninPhase.WAITING_FOR_LOGIN)
    alone = refresh.returned[first_wait - 1]
    assert first_wait >= 2 and alone.strays == [] and [r.name for r in alone.rows if r.pids] == ["T"]
def test_waiting_only_after_target_running(tmp_path):
    # refresh items: [T not running, T not running, T alone, T alone created 5]
    phases = []; flow.on_phase = lambda p, m: phases.append((p, refresh.calls))
    flow.run(plan_signin(snap0, "T"), threading.Event())
    assert next(c for p, c in phases if p is SigninPhase.WAITING_FOR_LOGIN) >= 3
def test_new_main_process_during_wait_warns(tmp_path):
    # refresh items: [T alone, T + argless 601, T + argless 601 created 5]
    assert any("pid 601" in w for w in flow.run(plan_signin(snap0, "T"), threading.Event()).warnings)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_signin.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `signin.py` and `ScriptedRefresh`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_signin.py -v`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/signin.py tests/helpers.py tests/test_signin.py
git commit -m "feat(tui): sign-in helper that quits every other Claude and always restores"
```

---

### Task 15: Refresh scheduler

**Files:**
- Create: `src/claude_instances/scheduler.py`, `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `LIVE_INTERVAL_S` (Task 9).
- Produces: `STATE_INTERVAL_S = 10`; `backoff_after(failures: int, base: int = LIVE_INTERVAL_S, cap: int = 3600) -> int` (`min(base * 2**failures, cap)`); `class LiveScheduler` with `__init__(self, now: Callable[[], float] = time.monotonic)`, `set_candidates(names: list[str])` (rows with a session cookie: SIGNED_IN or REJECTED, so a rejected row is retried at backoff cadence; dropped names forget their state), `request_now(name)`, `next_due() -> str | None` (earliest due time <= now; new names due immediately; None while a lookup is in flight), `start(name)`, `finish(name, ok: bool)` (success: due in `LIVE_INTERVAL_S`, failures reset; failure: due in `backoff_after(failures)`; always clears in-flight). The scheduler is not thread-safe by design: callers use it from the event loop only (Task 17 calls `start` before spawning the worker and `finish` through `call_from_thread`).

- [ ] **Step 1: Write the failing tests**

```python
def test_new_candidates_due_immediately_one_at_a_time():
    t = [0.0]; s = LiveScheduler(now=lambda: t[0]); s.set_candidates(["A", "B"])
    assert s.next_due() == "A"; s.start("A"); assert s.next_due() is None
    s.finish("A", ok=True); assert s.next_due() == "B"
def test_success_schedules_five_minutes(): ...     # after finish ok, A not due at t+299, due at t+300
def test_failure_backs_off(): ...                  # 1 failure -> due at +600; 2 -> +1200
def test_backoff():
    assert [backoff_after(n) for n in (0, 1, 2, 3, 4)] == [300, 600, 1200, 2400, 3600]
def test_request_now_overrides(): ...
def test_dropped_candidate_forgotten(): ...
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_scheduler.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `scheduler.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_scheduler.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/scheduler.py tests/test_scheduler.py
git commit -m "feat(tui): serial live-usage scheduler with backoff"
```

---

### Task 16: Backend (state, live lookups, guarded actions)

**Files:**
- Create: `src/claude_instances/backend.py`, `tests/test_backend.py`

**Interfaces:**
- Consumes: Tasks 1 to 9 and 11 to 15 (only the `Lookup` Protocol from Task 9, not Task 10).
- Produces: `PENDING_LAUNCH_S = 30`; `class Backend`:
  - `__init__(self, apps_dirs: list[Path], config_dir: Path, log_dir: Path, support_dir: Path, actions: SystemActions, lookup: Lookup | None, default_data_dir: Path, default_claude_app: Path, sleep=time.sleep, now=lambda: datetime.now(timezone.utc), monotonic=time.monotonic)`; attributes `apps_dirs`, `cached_only` (`lookup is None`).
  - `snapshot() -> Snapshot`: discover, `actions.processes()`, cookies, accounts, order, cache; passes the `live`, `account_info`, `live_errors` and `rejected` it holds; clears `pending_launches` entries whose row is now running.
  - `live_lookup(name: str) -> LookupResult`: reads the row's current `account` and `session_created` first, then `lookup.lookup(data_dir)`. On usage: `live[name] = LiveEntry(account, usage)`, `usage_cache.put` + `save`, `account_info[account] = AccountInfo(email, org)`, clears `rejected[name]` and `live_errors[name]`, sets `key_status = "ok"`. On `rejected`: `rejected[name] = RejectedMark(account, session_created)`. On error: `live_errors[name] = error`; `"cannot decrypt cookies (wrong key?)"` sets `key_status = "bad"` unless it is already `"ok"`. Every step is inside `try/except Exception` so a failure (for example `usage_cache.save` raising `OSError`) becomes `live_errors[name] = f"{step} failed ({type(e).__name__})"` and the method returns normally.
  - `key_status: Literal["none", "unverified", "ok", "bad"]` (`"none"` when cached-only, else `"unverified"` until the first result).
  - `launch(name) -> ActionResult`: refuses with `"launch requested <n>s ago; waiting for it to appear"` when `pending_launches[name]` is younger than `PENDING_LAUNCH_S` (a second `open -n` would USR1 the owning watchdog and make a later Cmd+Q reopen the instance); else `guarded_launch`, recording `pending_launches[name] = monotonic()` on success. `launch_all() -> list[tuple[str, ActionResult]]` passes `launch_one` that goes through the same pending check. `rearm(name)`, `quit(name)`, `front(name)` wrap the Task 13 functions.
  - `move(name, delta)`, `mark_expected(name)`.
  - `pending_restore() -> list[str]`: the names in the restore file, but only when its owner pid is None or `not actions.is_alive(owner)`, so a second TUI never offers to restore mid-login.
  - `make_signin_flow(on_phase) -> SigninFlow` (overridable in tests).

- [ ] **Step 1: Write the failing tests**

`tests/test_backend.py` builds a `Backend` over `tmp_path` fixtures (`make_launcher_app`, `make_data_dir`) with `FakeSystemActions` and a `FakeLookup(results: dict[str, LookupResult])`.

```python
def test_live_lookup_success_stores_live_cache_and_info(backend, fake_lookup):
    r = backend.live_lookup("Claude C")
    row = row_of(backend.snapshot(), "Claude C")
    assert row.usage.source is Source.LIVE and row.email == "user@example.com" and backend.key_status == "ok"
    assert UsageCache.load(cfg / "usage-cache.json").get("Claude C", A) is not None
def test_live_lookup_rejected_records_mark(backend): ...          # rejected=True -> row.session is REJECTED
def test_live_lookup_cache_write_error_is_contained(backend, monkeypatch):
    monkeypatch.setattr(UsageCache, "save", lambda self, p: (_ for _ in ()).throw(OSError("disk")))
    backend.live_lookup("Claude C")
    assert row_of(backend.snapshot(), "Claude C").live_error == "cache write failed (OSError)"
def test_email_follows_account(backend):
    backend.live_lookup("Claude C"); write_config(data_dir("Claude C"), account=B)
    assert row_of(backend.snapshot(), "Claude C").email is None
def test_key_status_bad_on_decrypt_error(backend): ...            # every result "cannot decrypt cookies (wrong key?)" -> "bad"
def test_pending_launch_blocks_second_launch(backend, fake):
    assert backend.launch("Claude I").ok
    r = backend.launch("Claude I")
    assert not r.ok and "waiting for it to appear" in r.message and len(fake.launched) == 1
def test_pending_restore_ignores_live_owner(backend, fake):
    write_restore(cfg / "signin-restore.txt", ["Claude C"], owner_pid=4242)
    fake.alive.add(4242); assert backend.pending_restore() == []
    fake.alive.discard(4242); assert backend.pending_restore() == ["Claude C"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_backend.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `backend.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_backend.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/backend.py tests/test_backend.py
git commit -m "feat(tui): backend for snapshots, live lookups and guarded actions"
```

---

### Task 17: Textual app and screens

**Files:**
- Create: `src/claude_instances/app.py`, `src/claude_instances/screens.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: `Backend` (Task 16), `LiveScheduler`, `STATE_INTERVAL_S` (Task 15), `plan_signin`, `SigninRefused` (Task 14), `AUTOMATION_HELP`, `QuitOutcome` (Task 13), `format_cell`, `level` (Task 9), `rebuild_command` (Task 6).
- Produces: `class InstancesApp(App)` with `__init__(self, backend: Backend)`, public `header_text: str` and `status_text: str` (tests assert these, not widget internals), `signin_thread: threading.Thread | None`, `signin_cancel: threading.Event | None`, and `finish_signin(timeout: float | None = None) -> None` (sets `signin_cancel` and joins `signin_thread`; no-op when none). `class InstancesScreen(Screen)` holds the table and the action bindings.
- Bindings:
  - On `InstancesScreen` (so any `ModalScreen` on top blocks them): `f` front, `l` launch, `q` quit instance, `w` re-arm watchdog, `a` launch all, `r` refresh usage now, `s` sign in, `d` details, `o` open watchdog log, `m` mark expected account, `shift+up`/`shift+down` move row. `check_action` also returns False for `f l q w a s m` and the moves while a sign-in is running.
  - On the app: `Binding("ctrl+q", "request_exit", priority=True)` and `Binding("ctrl+c", "request_exit", priority=True)`. `ENABLE_COMMAND_PALETTE = False`. `action_quit` is overridden to call `action_request_exit`, so no built-in path exits without restoring.
- Table: `DataTable` (`cursor_type="row"`, row key = name) with columns `#`, `Instance`, `State`, `Account`, `5h`, `Week`.
  - State: `● open` / `○ closed` / `DUPLICATE` / `● open (not started by a launcher)` for argless.
  - Account: email or short id, `signed out`, `last: <short id>` for signed-out rows with a known account, `session rejected`, `⚠ ... (shared with X)`, `⚠ ... (expected <short id>)`, plus the row's `live_error` in dim text.
  - Usage cells come from `format_cell`, with classes `warn`/`crit` from `level()` and a dim style for cached numbers.
- `header_text`: `"Claude instances    <live>    refreshed HH:MM:SS"` where `<live>` is `cached only (started without --key-fd)` (`key_status == "none"`), `live usage: checking key` (`"unverified"`), `live usage ✓` (`"ok"`), or `live usage: key cannot decrypt cookies; showing cached` (`"bad"`).
- Refresh and live lookups:
  - `set_interval(STATE_INTERVAL_S, self.refresh_state)`; `refresh_state` catches every exception into `status_text = f"refresh failed ({type(e).__name__})"`.
  - A 1 s tick on the event loop calls `scheduler.next_due()`. When a name is due it calls `scheduler.start(name)` on the loop, then `run_worker(body, thread=True, group="live", exit_on_error=False)`. The body runs `backend.live_lookup(name)` in `try`/`finally`, and the `finally` calls `self.call_from_thread(scheduler.finish, name, ok)` (guarded against the app having exited). The scheduler is only ever touched on the event loop.
- Blocking actions (quit, launch all, re-arm, front) run in `run_worker(..., thread=True, exit_on_error=False)`. Each body catches `Exception` into `status_text = f"<action> failed ({type(e).__name__})"` and posts results via `call_from_thread`. Key handlers never sleep on the event loop. Account details (email, org) never appear in a status message, log or exception text.
- Behaviors:
  - `f`: on a non-running row the status is `"<name> is not running; press l to launch"`; on a duplicate row it says so; on a running row it calls `backend.front` and shows `AUTOMATION_HELP` when denied.
  - `l`: calls `backend.launch`.
  - `q`: asks `ConfirmScreen`, then `backend.quit`; on DID_NOT_QUIT the status offers `w` (re-arm); on WATCHDOG_STUCK it shows that message.
  - `s`: calls `plan_signin`, showing a `SigninRefused` as the status. Then a `ConfirmScreen` lists `quit_rows`, `watchdog_rows` and `not_restorable` ("will be quit and not relaunched"). Then the flow runs on a plain `threading.Thread(name="signin", daemon=False)` stored in `signin_thread`, with `SigninScreen` showing phase text and warnings, and `escape` setting `signin_cancel`. On success with a new account, a `ConfirmScreen("Record <email or short id> as the expected account for <name>?")` calls `mark_expected`; `restore_failed` entries are listed in the status.
  - `o`: `with self.suspend(): subprocess.run(shlex.split(os.environ.get("PAGER") or "less") + [str(log)])`.
- Exit:
  - `action_request_exit` exits immediately when no sign-in is running. Otherwise it sets `signin_cancel`, sets `status_text = "Restoring instances before exit..."` and `exit_after_signin = True`; when the sign-in thread's completion reaches the loop, the app calls `self.exit()`.
  - `on_mount` registers SIGTERM and SIGHUP through `asyncio.get_running_loop().add_signal_handler` to `action_request_exit`.
  - Because the sign-in thread is non-daemon and `cli.main` calls `finish_signin()` after `app.run()` returns (Task 18), the restore still runs if the loop is torn down early. If the process is killed with SIGKILL, the restore file remains and is offered on next start.
- `screens.py`:
  - `ConfirmScreen(ModalScreen[bool])` (y/n).
  - `DetailsScreen(ModalScreen)`: display name, account, expected account, data folder, launcher path, session cookie expiry, watchdog status, the launcher's rebuild command from `rebuild_command` (or "cannot be rebuilt with new-claude"), "old one-shot launcher" or "launcher predates the argless rule" when applicable, the last 20 log lines, and the caveat "A present cookie means the app believes it is signed in; a server-side revocation shows only when the app next talks to the server."
  - `SigninScreen(ModalScreen)`.
  - `RestoreOfferScreen`, shown on start when `backend.pending_restore()` is non-empty ("Relaunch N instances quit by an interrupted sign-in?"); yes launches each through `backend.launch` in order, then clears the restore file, or keeps the failed names.

- [ ] **Step 1: Write the failing tests**

`tests/test_app.py` builds a `Backend` as in Task 16 with `lookup=None` unless stated, `sleep=lambda s: None`. `BlockingFlow` (in the test file) has `run(plan, cancelled)` that waits on `cancelled` or `release()`, then sets `restored = True`.

```python
async def test_table_renders_rows_and_cached_header(backend):
    async with InstancesApp(backend).run_test() as pilot:
        table = pilot.app.query_one(DataTable)
        assert table.row_count == 3 and "cached only" in pilot.app.header_text
        assert "DUPLICATE" in str(table.get_row("Claude Dup")[2])
        assert "shared with" in str(table.get_row("Claude C")[3])
async def test_live_lookup_marks_row_live(backend_with_fake_lookup): ...   # after the tick, 5h cell has no "old"; header "live usage ✓"
async def test_header_reflects_bad_key(backend_with_decrypt_errors): ...   # header contains "key cannot decrypt cookies"
async def test_launch_key_on_closed_row_calls_open_n(backend, fake):
    ...; await pilot.press("l"); await pilot.app.workers.wait_for_complete(); assert fake.launched == [app_path("Claude I")]
async def test_launch_key_on_running_row_is_refused_with_message(backend, fake): ...   # status_text contains "already running"
async def test_front_on_closed_row_offers_launch(backend): ...                         # status_text "...not running; press l to launch"
async def test_front_denied_shows_help(backend, fake): ...                             # fake.front_result = ActionResult(False, AUTOMATION_HELP)
async def test_quit_asks_then_quits_off_loop(backend, fake):
    ...; await pilot.press("q"); await pilot.press("y"); await pilot.app.workers.wait_for_complete()
    assert ("send_quit", 501) in fake.calls
async def test_move_row_rewrites_order_file(backend): ...     # shift+down -> order.txt changed
async def test_restore_offer_on_pending_file(backend): ...   # restore file with a dead owner pid -> RestoreOfferScreen on top
async def test_action_keys_blocked_during_signin(backend, fake):
    flow = BlockingFlow(); backend.make_signin_flow = lambda on_phase: flow
    async with InstancesApp(backend).run_test() as pilot:
        await start_signin(pilot, "Claude I")
        launched_before = list(fake.launched)
        await pilot.press("a"); await pilot.press("l"); await pilot.pause()
        assert fake.launched == launched_before
        flow.release(); pilot.app.finish_signin()
async def test_exit_during_signin_restores_first(backend):
    flow = BlockingFlow(); backend.make_signin_flow = lambda on_phase: flow
    async with InstancesApp(backend).run_test() as pilot:
        await start_signin(pilot, "Claude I")
        await pilot.press("ctrl+q"); await pilot.pause()
        assert flow.cancelled.is_set() and pilot.app.is_running and "Restoring" in pilot.app.status_text
        pilot.app.signin_thread.join(5); await pilot.pause()
        assert flow.restored and not pilot.app.is_running
async def test_builtin_quit_routes_through_request_exit(backend):
    # same setup; call pilot.app.action_quit() instead of pressing ctrl+q -> cancelled set, app still running
async def test_finish_signin_cancels_and_joins(backend):
    # start a sign-in, then call finish_signin(timeout=5) directly -> flow.restored and not signin_thread.is_alive()
async def test_signin_offers_expected_account(backend): ...  # flow returns new_account -> ConfirmScreen; "y" writes expected-accounts.json
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_app.py -v`
Expected: FAIL, import error

- [ ] **Step 3: Implement `screens.py` and `app.py`**

Each action handler reads the row under the cursor, starts a thread worker that calls the matching `Backend` method, sets `status_text` from the result, and calls `refresh_state()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_app.py -v`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/app.py src/claude_instances/screens.py tests/test_app.py
git commit -m "feat(tui): Textual instances table, details, confirmations and sign-in screens"
```

---

### Task 18: CLI wiring and a scripted stub-only smoke test

**Files:**
- Modify: `src/claude_instances/cli.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Consumes: `read_secret_fd` (Task 2), `derive_key` (Task 4), `UrllibHttpClient` (Task 5), `UsageLookup` (Task 10), `MacSystemActions` (Task 13), `Backend` (Task 16), `InstancesApp` (Task 17), `paths` (Task 1).
- Produces: `cli.parse_args(argv) -> argparse.Namespace`; `cli.build_backend(args) -> Backend` (key from `--key-fd` -> `derive_key` -> `UsageLookup(UrllibHttpClient(), key)`, else `lookup=None`; `--apps` list or `paths.apps_dirs()`); `main` runs `app = InstancesApp(build_backend(args)); app.run()` and then, always (in a `finally`), `app.finish_signin()`, so a sign-in still running when the loop ends (SIGHUP teardown, an exception) is cancelled and its restore completes before the process exits; returns 0; `probe` as in Task 5.

- [ ] **Step 1: Write the failing tests**

```python
def test_no_key_fd_is_cached_only(tmp_path):
    b = build_backend(parse_args(["--apps", str(tmp_path)]))
    assert b.cached_only and b.apps_dirs == [tmp_path]
def test_key_fd_enables_live(tmp_path):
    r, w = os.pipe(); os.write(w, b"pw"); os.close(w)
    assert not build_backend(parse_args(["--key-fd", str(r), "--apps", str(tmp_path)])).cached_only
def test_empty_key_fd_falls_back_to_cached(tmp_path): ...
def test_main_calls_finish_signin_after_run(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(InstancesApp, "run", lambda self: calls.append("run") or (_ for _ in ()).throw(RuntimeError("x")))
    monkeypatch.setattr(InstancesApp, "finish_signin", lambda self, timeout=None: calls.append("finish"))
    with pytest.raises(RuntimeError): main(["--apps", str(tmp_path)])
    assert calls == ["run", "finish"]
async def test_smoke_against_stub_apps_dir(tmp_path, monkeypatch):
    # two stub launchers via make_launcher_app pointing at make_data_dir folders under tmp_path; real MacSystemActions (reads ps only)
    monkeypatch.setenv("CLAUDE_INSTANCES_CONFIG_DIR", str(tmp_path / "cfg"))
    app = InstancesApp(build_backend(parse_args(["--apps", str(tmp_path / "Apps")])))
    async with app.run_test() as pilot:
        assert app.query_one(DataTable).row_count == 2 and "cached only" in app.header_text
        await pilot.press("d"); assert isinstance(app.screen, DetailsScreen)
        await pilot.press("escape"); await pilot.press("ctrl+q")
    assert not app.is_running
```

The smoke test presses no key that launches, quits or signals anything.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `cli.py`**

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all passed

- [ ] **Step 5: Commit**

```bash
git add src/claude_instances/cli.py tests/test_cli.py
git commit -m "feat(tui): claude-instances entry point with --key-fd and --apps"
```

---

### Task 19: zsh argless-default rule in `new-claude` and the launcher

**Files:**
- Modify: `bin/new-claude.sh`: the `# ---- config` block, `instance_running_for()`, the launcher header heredoc (`<<HDR`), and `instance_pid()` inside the `<<'BODY'` heredoc (locate by these markers; line numbers shift)
- Create: `tests/lib/stub-claude.sh`, `tests/test-argless.sh`, `tests/test_generated_launcher.py`

**Interfaces:**
- Produces: in `new-claude.sh` config: `DEFAULT_DATA_DIR="${CLAUDE_LAUNCHER_DEFAULT_DATA_DIR:-$SUPPORT_DIR/Claude}"` (comment: test hook); the launcher header heredoc gains `DEFAULT_DATA_DIR="$DEFAULT_DATA_DIR"` (baked at build time, so launchers opened through `open` see it too; Task 6 uses this line to detect `has_argless_rule`). `instance_running_for "$capp" "$datadir"` and the launcher's `instance_pid` additionally match, only when the data dir equals `DEFAULT_DATA_DIR`, an argless main process: the binary at the start of the command or after a space, followed by end of line or `" -"`, with no `--user-data-dir=` and no `--type=` anywhere on the line.
- `tests/lib/stub-claude.sh`: sourced by new tests; expects `TMP`, `APPS`, `STUB_APP`, `STUB_BIN` set; defines `build_stub_app` (the same stub bundle and Info.plist as `tests/test-watchdog.sh` lines 53-85), `stub_running_for DATA_DIR`, `stub_pids` (every stub), `argless_stub_count` (stub lines with no arguments or flags only, no `--user-data-dir=`, no `--type=`; same rule as above, via `ENVIRON`), `kill_stub`, `watchdog_pids_for LAUNCHER`, `kill_watchdogs_for LAUNCHER`, `ok`, `bad`. A comment above `build_stub_app` records a known quirk: the stub's single-instance check matches its own arguments with awk `index($0, ENVIRON["MA"])`, and macOS awk returns 1 for `index(s, "")`, so an argless stub (empty `MA`) exits at once whenever any other stub is already running. Tests must start an argless stub only when no other stub runs, as the plan's order does. `tests/test-watchdog.sh` is left untouched.

- [ ] **Step 1: Write the failing test script `tests/test-argless.sh`**

Same shape as `tests/test-watchdog.sh` (header comment, `TMP=$(mktemp -d /tmp/claude-argless-test.XXXXXX)`, `trap cleanup EXIT INT TERM`, `CLAUDE_LAUNCHER_*_SECS` set to 1/2/1, `Results: N passed, M failed`, exit status from `FAIL`). Export `CLAUDE_LAUNCHER_CLAUDE_LOG="$TMP/main.log"` and `CLAUDE_LAUNCHER_SHIPIT_DIR="$TMP/ShipIt"` (created empty) so exit diagnostics never read the real `main.log` or ShipIt cache. Set `export CLAUDE_LAUNCHER_DEFAULT_DATA_DIR="$HOME/Library/Application Support/claude-argless-test-default"` before building two launchers against the stub: `"Claude ArglessDefault" --dir claude-argless-test-default` and `"Claude ArglessOther" --dir claude-argless-test-other`. Between tests: kill both launchers' watchdogs and all stubs, `sleep 1`. Twelve checks:

```
Test 1: --reopen-all treats an argless stub as the running default instance
  "$STUB_BIN" & ; sleep 1
  1  "$NEW_CLAUDE" --reopen-all --apps "$APPS" output has "Claude ArglessDefault: already running"
  2  same output has "Claude ArglessOther: reopening"
  3  after sleep 3: argless_stub_count == 1 and ! stub_running_for "$CLAUDE_LAUNCHER_DEFAULT_DATA_DIR"
Test 2: the default launcher adopts an argless stub instead of launching a second one
  "$STUB_BIN" & ; "$LAUNCHER_DEFAULT" & ; sleep 4
  4  its log has "adopting already-running instance"
  5  stub_pids | wc -l == 1
  6  kill_stub; sleep 6; the launcher's watchdog has exited
Test 3: a non-default launcher still ignores an argless stub
  "$STUB_BIN" & ; "$LAUNCHER_OTHER" & ; sleep 4
  7  stub_running_for "$HOME/Library/Application Support/claude-argless-test-other"
  8  stub_pids | wc -l == 2
Test 4: a --type= helper line never counts as the argless instance
  9  with only "$STUB_BIN" --type=renderer running: --reopen-all output has "Claude ArglessDefault: reopening"
Test 5: a non-flag first argument is not argless
  10 with only "$STUB_BIN" /tmp/claude-argless-script.js running: --reopen-all output has "Claude ArglessDefault: reopening"
Test 6: ShipIt-style relaunch: the default instance dies and comes back argless within the grace window
  "$LAUNCHER_DEFAULT" & ; sleep 4   (it launches a stub with --user-data-dir=<default test dir>)
  kill_stub; sleep 0.5; "$STUB_BIN" & ; sleep 5
  11 its log has "instance relaunched itself"
  12 stub_pids | wc -l == 1 (the watchdog did not launch a second one)
```

`tests/test_generated_launcher.py` (pytest, cross-language check): creates `tmp_path/FakeClaude.app/Contents/Info.plist` only, runs `bin/new-claude.sh "Claude GenTest" --apps <tmp_path>/Apps --claude-app <tmp_path>/FakeClaude.app --dir claude-gen-test` with env `CLAUDE_LAUNCHER_DEFAULT_DATA_DIR=<support dir>/claude-gen-test`, then asserts `discover_launchers([tmp_path / "Apps"])` returns one launcher with `is_watchdog`, `has_argless_rule`, `data_dir == Path.home() / "Library/Application Support/claude-gen-test"` and `claude_app == tmp_path / "FakeClaude.app"`. It launches nothing (the launcher is built, never run).

- [ ] **Step 2: Run it to verify it fails**

Run: `tests/test-argless.sh`
Expected: checks 1, 3, 4, 5, 11 and 12 report `✗` (the default launcher is reopened or launches a second stub); the others may already pass.
Run: `.venv/bin/pytest tests/test_generated_launcher.py -v`
Expected: FAIL (`has_argless_rule` is False)

- [ ] **Step 3: Implement the rule in both awk programs**

Add `DEFAULT_DATA_DIR` to the config block and to the launcher header heredoc. In `instance_running_for` (and identically in the launcher's `instance_pid`, which also prints `$1`), pass `MDEF` through the environment and extend the awk body:

```awk
BEGIN { b = ENVIRON["MB"]; d = ENVIRON["MD"]; isdef = ENVIRON["MDEF"] }
(j = index($0, b)) && (j == 1 || substr($0, j - 1, 1) == " ") {
  if (i = index($0, d)) {
    r = substr($0, i + length(d), 1)
    if (r == "" || r == " ") { f = 1; exit }
  } else if (isdef == "1" && !index($0, "--user-data-dir=") && !index($0, "--type=")) {
    a = substr($0, j + length(b), 2)
    if (a == "" || a == " -") { f = 1; exit }
  }
}
END { exit f ? 0 : 1 }
```

The `ps -axo command=` caller in `instance_running_for` has no pid column, so `j == 1` holds there; in `instance_pid` (`pid=,command=`) the binary follows a space. `MDEF=$([ "$datadir" = "$DEFAULT_DATA_DIR" ] && echo 1 || echo 0)` (in the launcher: compare `$DATA_DIR`). Search strings keep going through `ENVIRON`, never `awk -v`. The existing `--user-data-dir` branch keeps its current behavior, apart from requiring the binary at a word start, which every real Claude line satisfies.

- [ ] **Step 4: Run the new test and the existing watchdog test**

Run: `tests/test-argless.sh`
Expected: `Results: 12 passed, 0 failed`
Run: `.venv/bin/pytest tests/test_generated_launcher.py -v`
Expected: 1 passed
Run: `tests/test-watchdog.sh`
Expected: `0 failed` (unchanged behavior)

- [ ] **Step 5: Commit**

```bash
git add bin/new-claude.sh tests/lib/stub-claude.sh tests/test-argless.sh tests/test_generated_launcher.py
git commit -m "fix: count an argless default Claude as the default-folder instance"
```

---

### Task 20: `new-claude --launch-all`

**Files:**
- Modify: `bin/new-claude.sh`, located by markers because Task 19 shifted the lines: the `# Usage:` header comment block, the `# ---- parse arguments` `case` (`--launch-all` sets `DO_LAUNCH_ALL=1`; `--apps` also sets `APPS_GIVEN=1`), the `sed -n '2,22p'` help line, a new `# ---- --launch-all mode` block right after the `# ---- --reopen-all mode` block, and the `Usage: new-claude` error lines
- Create: `tests/test-launch-all.sh`

**Interfaces:**
- Consumes: `instance_running_for` (Task 19), `tests/lib/stub-claude.sh` (Task 19).
- Produces: `new-claude --launch-all [--apps DIR]`: scans `--apps DIR` when given, else both `/Applications` and `~/Applications` (same set the TUI scans; `--list` and `--reopen-all` keep scanning only `$APPS_DIR`); reads `${CLAUDE_INSTANCES_CONFIG_DIR:-$HOME/.config/claude-instances}/order.txt` with `while IFS= read -r line || [ -n "$line" ]` (missing file is fine); discovers launchers as `--list` does into an associative array `APP_OF[name]=app`; order = names from the file that exist (first occurrence only), then the remaining names piped through `LC_ALL=C sort`; per name prints `   • $name: already running`, or `   • $name: launching` then `open -n "$app"`, or `   • $name: could not determine data dir; skipping`; sleeps `LAUNCH_GAP=3` seconds between consecutive launches only; prints `   (no launchers found)` when none.

- [ ] **Step 1: Write the failing test script `tests/test-launch-all.sh`**

Same shape as Task 19's script; sources `tests/lib/stub-claude.sh`; `export CLAUDE_INSTANCES_CONFIG_DIR="$TMP/cfg"`; builds three stub launchers `Claude LA One`, `Claude LA Two`, `Claude LA Three` (data dirs `claude-la-test-{one,two,three}`); writes `order.txt` with `Claude LA Three`, `Claude LA Ghost`, `Claude LA Three`, `Claude LA One`. Ten checks:

```
Test 1: launches in order.txt order, then the rest, with a 3 s gap
  START=$(date +%s); OUT=$("$NEW_CLAUDE" --launch-all --apps "$APPS"); ELAPSED=$(( $(date +%s) - START ))
  1  the "launching" lines name Three, One, Two in that order
  2  OUT does not mention Ghost
  3  ELAPSED -ge 6 (two gaps between three launches)
  4-6 after sleep 12: stub_running_for each of the three data dirs
Test 2: a second --launch-all skips running instances and launches nothing
  7  OUT has three "already running" lines and no "launching"
  8  stub_pids | wc -l == 3
Test 3: no order file launches everything in C order
  rm order.txt; kill all; run
  9  "launching" lines are One, Three, Two
Test 4: a last order.txt line without a trailing newline is honored
  printf 'Claude LA Two\nClaude LA One' > order.txt; kill all; run
  10 "launching" lines are Two, One, Three
```

Cleanup kills the three watchdogs by launcher path and the stubs, and removes the three logs and locks. The `~/Applications` default scan is not exercised by tests (it would touch the user's real folder); it is checked by reading the diff.

- [ ] **Step 2: Run it to verify it fails**

Run: `tests/test-launch-all.sh`
Expected: `Unknown option: --launch-all`, all checks `✗`

- [ ] **Step 3: Implement `--launch-all`**

Model the block on the `# ---- --reopen-all mode` block. Add `new-claude --launch-all` to the header usage block and to the usage error line, and widen the help `sed` range from `'2,22p'` to end on the header's last line ("Re-running with the same name safely rebuilds that launcher.") after the insertion.

- [ ] **Step 4: Run the new test and both existing tests**

Run: `tests/test-launch-all.sh`
Expected: `Results: 10 passed, 0 failed`
Run: `bin/new-claude.sh --help | tail -n 1`
Expected: `Re-running with the same name safely rebuilds that launcher.`
Run: `tests/test-argless.sh && tests/test-watchdog.sh`
Expected: both `0 failed`

- [ ] **Step 5: Commit**

```bash
git add bin/new-claude.sh tests/test-launch-all.sh
git commit -m "feat: new-claude --launch-all launches instances in order.txt order"
```

---

### Task 21: README and Dock app

**Files:**
- Modify: `README.md` (new section "Instances TUI" between "Surviving auto-updates" and "ChatGPT instances"; add `new-claude --launch-all` to "Usage"; add Python and pipx to "Requirements"; no change to "Install")

**Interfaces:**
- Consumes: the `av bless` form confirmed at Checkpoint A; flags from Task 18; `--launch-all` from Task 20; `rebuild_command` (Task 6).

- [ ] **Step 1: Write the "Instances TUI" section**

Cover, in this order, with one short code block each:
1. What it shows (one paragraph; the primary job is usage) and the keys (`f l q w a r s d o m`, `shift+up/down`, `ctrl+q`).
2. Install: `pipx install .` from the repo (or `pip install -e '.[dev]'` in a venv for development); Python 3.11 or newer.
3. Live usage setup: the `security ... | av save --stdin CLAUDE_SAFE_STORAGE` line, `av bless` (the form that worked at Checkpoint A), the run line `av inject --mode=fd +CLAUDE_SAFE_STORAGE:3 -- claude-instances --key-fd 3`; without the key it runs cached-only and says so in the header; re-bless after every `pipx` reinstall because the wrapper path changes; the usage API is unofficial and may change.
4. What is stored where (`~/.config/claude-instances/`: order.txt, expected-accounts.json, usage-cache.json with numbers and times only, signin-restore.txt) and that nothing secret is written to disk.
5. Bring to front needs Automation permission for System Events (one line on where to grant it).
6. The sign-in helper in three sentences, including that it quits every other Claude, that the restore file survives a crash and is offered on next start.
7. **Rebuild your launchers** so `--reopen-all`, `--launch-all`, the watchdog and the TUI apply the argless-default rule. Rebuild each one with its existing data folder and apps folder, never with the name alone (the default folder would change):
   `new-claude "<Name>" --dir "<last part of the data path that new-claude --list shows>" [--apps "<folder holding the .app, if not /Applications>"]`
   Example: a launcher whose data is `~/Library/Application Support/Claude` is rebuilt with `--dir Claude`. The TUI's details view (`d`) shows the exact command for each launcher.
8. Dock app: `osacompile -o "/Applications/Claude All.app" -e "do shell script \"$HOME/.local/bin/new-claude --launch-all\""` (absolute path because `do shell script` has a minimal PATH; `--launch-all` scans both `/Applications` and `~/Applications`).

Also add under "Usage": `new-claude --launch-all` with one sentence about `order.txt`. Under "Requirements": Python 3.11+ and pipx for the TUI.

- [ ] **Step 2: Proofread**

Run: `git diff -U0 README.md | grep '^+' | grep -c $'\xe2\x80\x94'`
Expected: `0` (no em dashes in added lines; pre-existing ones elsewhere stay as they are); code fences balanced.

- [ ] **Step 3: Rebuild the Dock app (ask first)**

Ask the user to confirm overwriting `/Applications/Claude All.app`; on yes run the `osacompile` line from the README. Expected: `osadecompile "/Applications/Claude All.app"` prints the one-line `do shell script`.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: instances TUI, launch-all, launcher rebuild and Dock app"
```

---

### Task 22: CHECKPOINT B (user runs the TUI end to end) and final verification

**Files:**
- Possibly modify: `src/claude_instances/actions.py` and `README.md`, only if a check fails.

- [ ] **Step 1: Prepare the rebuild commands (read-only)**

Run: `bin/new-claude.sh --list` and `bin/new-claude.sh --list --apps ~/Applications`
From each entry build `new-claude "<name>" --dir "<basename of data>"` plus `--apps "<dir of app>"` when not `/Applications`. Any launcher whose data path is not directly under `~/Library/Application Support` is listed as "cannot be rebuilt with new-claude; leave as is". The agent does not run the rebuilds (they stop running watchdogs).

- [ ] **Step 2: Stop and hand the end-to-end check to the user**

The executing agent does not launch, quit or signal any real instance and does not run the TUI without `--apps`. Post to the user:

1. Run the rebuild commands from Step 1, one per launcher (each stops that launcher's watchdog; the instance keeps running). Then run `new-claude --reopen-all` so every running instance is adopted by a fresh watchdog.
2. Run `av inject --mode=fd +CLAUDE_SAFE_STORAGE:3 -- claude-instances --key-fd 3`; confirm the header says live usage and every signed-in row gets numbers within a minute, without the "old" suffix.
3. Pick one idle instance, press `q`, confirm `y`. Expect the row to go `○ closed` within 30 s and its watchdog log (`~/Library/Logs/claude-instances/<name>.log`) to end with "watchdog stopped by signal" and not reopen it.
4. Press `l` on that row; confirm it comes back and details (`d`) shows a live watchdog.
5. Optional: with two instances running and a signed-out one, run `s` on the signed-out one; confirm the others are quit, the login lands in the target, the expected-account prompt appears, and the others come back.

- [ ] **Step 3: Act on the result**

If the quit in step 3 hung or the instance did not quit cleanly, switch `send_quit` to the `NSRunningApplication.terminate()` fallback described in Task 13, re-run `tests/test_actions.py`, and repeat step 3 with the user. Otherwise record "SIGTERM verified clean on Claude <version>" in the README's TUI section.

- [ ] **Step 4: Full verification**

Run: `.venv/bin/pytest -q && tests/test-argless.sh && tests/test-launch-all.sh && tests/test-watchdog.sh`
Expected: all pass.
Run: `git grep -n -I -E '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' -- . ':!docs' | grep -v -E '11111111|22222222|aaaaaaaa|bbbbbbbb'`
Expected: no output (only made-up ids in the tree).
Run: `git grep -n -I -E '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' -- . ':!docs' | grep -v -E '@example\.(com|org)'`
Expected: no output (no real-looking email addresses).
Run: `git grep -n -i -w 'mise' -- . ':!docs/superpowers'`
Expected: no output.
Run: `git grep -n -I $'\xe2\x80\x94' -- src tests pyproject.toml`
Expected: no output (the no-data glyph is written as `"\N{EM DASH}"`).

- [ ] **Step 5: Commit any follow-up and report**

```bash
git add src tests README.md pyproject.toml
git commit -m "chore(tui): end-to-end verification follow-ups"
```

Skip the commit if nothing changed. Report to the user: tests run, the quit mechanism verified, and the reminder that `~/.local/bin/claude-instances` must be re-blessed after any reinstall.

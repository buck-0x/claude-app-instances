"""The entry point: argument wiring, the sign-in teardown and a stub-only smoke run.

Nothing here launches, quits or signals a process; the smoke test uses stub
launchers under tmp_path and the real MacSystemActions only reads `ps`.
"""

import os
from pathlib import Path

import pytest
from textual.widgets import DataTable

from claude_instances import paths
from claude_instances.actions import MacSystemActions
from claude_instances.api import UsageLookup
from claude_instances.app import InstancesApp
from claude_instances.cli import build_backend, main, parse_args
from claude_instances.screens import DetailsScreen
from helpers import make_data_dir, make_launcher_app, pipe_with


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_INSTANCES_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CLAUDE_LAUNCHER_DEFAULT_DATA_DIR", str(tmp_path / "Support/Claude"))
    monkeypatch.setattr(paths, "log_dir", lambda: tmp_path / "Logs")


def test_no_key_fd_is_cached_only(tmp_path):
    b = build_backend(parse_args(["--apps", str(tmp_path)]))
    assert b.cached_only and b.lookup is None
    assert b.apps_dirs == [tmp_path]
    assert b.config_dir == tmp_path / "cfg"
    assert b.default_data_dir == tmp_path / "Support/Claude"
    assert b.log_dir == tmp_path / "Logs"
    assert isinstance(b.actions, MacSystemActions)


def test_default_apps_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "apps_dirs", lambda: [tmp_path / "A", tmp_path / "B"])
    assert build_backend(parse_args([])).apps_dirs == [tmp_path / "A", tmp_path / "B"]


def test_repeated_apps(tmp_path):
    b = build_backend(parse_args(["--apps", str(tmp_path / "A"), "--apps", str(tmp_path / "B")]))
    assert b.apps_dirs == [tmp_path / "A", tmp_path / "B"]


def test_key_fd_enables_live(tmp_path):
    fd = pipe_with(b"pw\n")
    b = build_backend(parse_args(["--key-fd", str(fd), "--apps", str(tmp_path)]))
    assert not b.cached_only
    assert isinstance(b.lookup, UsageLookup)
    # UsageLookup derives the AES key itself, so it is handed the raw password.
    assert b.lookup._key.reveal() == "pw"
    with pytest.raises(OSError):
        os.fstat(fd)  # the key fd is consumed and closed


def test_empty_key_fd_falls_back_to_cached(tmp_path):
    fd = pipe_with(b"")
    assert build_backend(parse_args(["--key-fd", str(fd), "--apps", str(tmp_path)])).cached_only


def test_closed_key_fd_falls_back_to_cached(tmp_path):
    r, w = os.pipe()
    os.close(r)
    os.close(w)
    assert build_backend(parse_args(["--key-fd", str(r), "--apps", str(tmp_path)])).cached_only


def test_non_utf8_key_fd_exits_with_fixed_message(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(InstancesApp, "run", lambda self: pytest.fail("must not start"))
    fd = pipe_with(b"\xff\xfe")
    assert main(["--key-fd", str(fd), "--apps", str(tmp_path)]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == "claude-instances: secret read from fd is not valid UTF-8\n"


def test_main_runs_app_then_returns_zero(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(InstancesApp, "run", lambda self: calls.append("run"))
    monkeypatch.setattr(InstancesApp, "finish_signin", lambda self, timeout=None: calls.append("finish"))
    assert main(["--apps", str(tmp_path)]) == 0
    assert calls == ["run", "finish"]


def test_main_calls_finish_signin_after_run(monkeypatch, tmp_path):
    calls = []

    def run(self):
        calls.append("run")
        raise RuntimeError("x")

    monkeypatch.setattr(InstancesApp, "run", run)
    monkeypatch.setattr(InstancesApp, "finish_signin", lambda self, timeout=None: calls.append("finish"))
    with pytest.raises(RuntimeError):
        main(["--apps", str(tmp_path)])
    assert calls == ["run", "finish"]


def test_probe_still_dispatches(monkeypatch, tmp_path):
    seen = {}

    def fake_probe(data_dir, fd, http):
        seen.update(data_dir=data_dir, fd=fd)
        return 7

    monkeypatch.setattr("claude_instances.probe.run_probe", fake_probe)
    assert main(["probe", "--key-fd", "9", "--data-dir", str(tmp_path)]) == 7
    assert seen == {"data_dir": tmp_path, "fd": 9}


async def test_smoke_against_stub_apps_dir(tmp_path):
    apps = tmp_path / "Apps"
    for name in ("Work", "Personal"):
        data = make_data_dir(tmp_path / "Support" / f"Claude-{name}",
                             account=None, session_expiry="none", history=None)
        make_launcher_app(apps, name, data)
    app = InstancesApp(build_backend(parse_args(["--apps", str(apps)])))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one(DataTable).row_count == 2
        assert "cached only" in app.header_text
        await pilot.press("d")
        assert isinstance(app.screen, DetailsScreen)
        await pilot.press("escape")
        assert not isinstance(app.screen, DetailsScreen)
        await pilot.press("ctrl+q")
    assert not app.is_running
